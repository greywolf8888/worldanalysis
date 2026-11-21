"""Timestamped local Chinese ASR. Keep all PCM, split near pauses and emit short reviewable cues."""
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=Path(os.environ.get("WORLDANALYSIS_COURSE_RUNTIME",ROOT/".runtime")).resolve()
sys.path.insert(0,str(RUNTIME/"site-packages"))
sys.path.insert(0,str(RUNTIME/"course-method-sensevoice/lib"))
import numpy as np

MODEL=RUNTIME/"course-method-sensevoice/model"
RATE=16000
MODEL_SHA="c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"
ENGINE=None
ENGINE_LANGUAGE=None


class AudioDecodeError(RuntimeError):
    def __init__(self, reason, start, samples):
        super().__init__(reason)
        self.start = start
        self.samples = samples


def read_media(path):
    p=subprocess.run(["ffprobe","-v","error","-protocol_whitelist","file,pipe","-show_entries","stream=index,codec_type,start_time:format=duration,start_time",
                      "-of","json",str(path)],capture_output=True,check=True)
    return json.loads(p.stdout)


def load_model(language="zh"):
    global ENGINE, ENGINE_LANGUAGE
    if ENGINE is not None and ENGINE_LANGUAGE == language:
        return
    import sherpa_onnx

    ENGINE=sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(MODEL/"model.int8.onnx"),tokens=str(MODEL/"tokens.txt"),
            language=language,use_itn=True,num_threads=3,provider="cpu")
    ENGINE_LANGUAGE=language


def cut_samples(data,mode):
    if mode=="fixed": return 30*RATE
    # Look for a quiet 250-ms interval near the target, including the entire interval in the PCM coverage.
    lo=int(25.5*RATE);hi=int(32*RATE); step=320
    samples=np.frombuffer(data,dtype="<i2",count=hi)
    frames=samples[lo:lo+((hi-lo)//step)*step].reshape(-1,step).astype(np.float32)
    energy=(frames*frames).mean(axis=1)
    smooth=np.convolve(energy,np.ones(13)/13,mode="valid")
    return lo+(int(np.argmin(smooth))+6)*step


def audio_windows(path,start,length,mode,audio_track=0):
    argv=["ffmpeg","-nostdin","-v","error","-xerror","-threads","2","-protocol_whitelist","file,pipe","-ss",str(start),"-i",str(path)]
    if length is not None: argv.extend(["-t",str(length)])
    argv.extend(["-map",f"0:a:{audio_track}","-vn","-ac","1","-ar",str(RATE),"-f","s16le","pipe:1"])
    proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    buf=bytearray(); samples=0; target=(30 if mode=="fixed" else 34)*RATE*2
    try:
        while True:
            piece=proc.stdout.read(target-len(buf))
            if not piece:
                if buf:
                    if len(buf)%2:
                        raise AudioDecodeError("Incomplete PCM sample", start, samples + len(buf)//2)
                    yield start+samples/RATE,bytes(buf)
                    samples+=len(buf)//2
                break
            buf.extend(piece)
            if len(buf)==target:
                take=cut_samples(buf,mode)
                yield start+samples/RATE,bytes(buf[:take*2])
                del buf[:take*2];samples+=take
        error=proc.stderr.read()
        if proc.wait():
            raise AudioDecodeError(error.decode("utf8","replace")[-600:].strip() or "FFmpeg 解码失败",
                                   start, samples)
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        proc.stdout.close();proc.stderr.close()


def recognize(job):
    start,raw=job
    pcm=np.frombuffer(raw,dtype="<i2")
    end=start+len(pcm)/RATE
    if not np.any((pcm<-4)|(pcm>4)):
        return {"start":start,"end":end,"text":"","tokens":[],"times":[],"digitally_silent":True}
    stream=ENGINE.create_stream()
    stream.accept_waveform(RATE,pcm.astype(np.float32)/32768)
    ENGINE.decode_stream(stream)
    r=stream.result; tokens=list(r.tokens); times=[min(end,max(start,start+x)) for x in r.timestamps]
    if len(tokens)!=len(times):raise ValueError("Timestamp/token mismatch")
    return {"start":start,"end":end,"text":r.text,"tokens":tokens,"times":times}


def transcribe(path,start,length,mode,workers,pool,audio_track=0):
    jobs=audio_windows(path,start,length,mode,audio_track)
    if workers==1:return [recognize(x) for x in jobs]
    pending=deque(); rows=[]
    for job in jobs:
        pending.append(pool.submit(recognize,job))
        if len(pending)>=workers*2:rows.append(pending.popleft().result())
    rows.extend(f.result() for f in pending)
    return rows


def captions(rows):
    result=[]; punct="，。！？；：,.!?;:"
    for r in rows:
        terms=[(w,ts) for w,ts in zip(r["tokens"],r["times"]) if w.strip() and not w.startswith("<|")]
        line="";begin=None;last=None
        def push(stop):
            if line.strip(punct+" ") and begin is not None and stop>begin:
                result.append({"start":round(begin,3),"end":round(stop,3),"text":line.strip()})
        for i,(word,ts) in enumerate(terms):
            if line and ((last is not None and ts-last>1.05) or
                         ((ts-begin>=6.4 or len(line)+len(word)>25) and ts>begin and word not in punct)):
                push(min(ts,last+0.6));line="";begin=None
            if begin is None:begin=ts
            line+=word;last=ts
            following=terms[i+1][1] if i+1<len(terms) else r["end"]
            if (word in "。！？!?" or (word in punct and len(line)>=9)) and following>begin:
                push(min(following,ts+0.65));line="";begin=None
        if line:push(min(r["end"],last+0.65))
    merged=[]
    for cue in result:
        if cue["end"]<=cue["start"]:continue
        prev=merged[-1] if merged else None
        if (prev and not prev["text"].endswith(("。","？","！","?","!")) and
            min(len(prev["text"].strip(punct)),len(cue["text"].strip(punct)))<=2 and
            cue["start"]-prev["end"]<=1.65 and cue["end"]-prev["start"]<=6.4 and
            len(prev["text"])+len(cue["text"])<=25):
            prev["end"]=cue["end"]
            prev["text"]+=cue["text"]
        else:merged.append(cue.copy())
    for a,b in zip(merged,merged[1:]):
        if a["end"]>b["start"]+0.001:raise ValueError("Overlapping subtitles")
    return merged
