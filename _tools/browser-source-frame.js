/* Normal authorized player -> bounded real JPEG. No URL, cookie or media file export. */
(() => {
  'use strict';
  window.WASourceFrameCapture = async config => {
    const source = Object.freeze({...config.source});
    const need = (ok, msg) => { if (!ok) throw Error(msg); };
    need(/^[0-9]{1,32}$/.test(source.file_id) && typeof source.source_revision === 'string', 'SOURCE_ID_REQUIRED');
    need(Number.isSafeInteger(config.requested_ms) && config.requested_ms >= 0, 'INVALID_FRAME_TARGET');
    need(/^[a-f0-9]{64}$/.test(config.helper_sha256), 'HELPER_FINGERPRINT_REQUIRED');
    need(/^[A-Za-z0-9]{16,128}$/.test(config.version_token), 'VERSION_TOKEN_REQUIRED');
    const pathGuard = () => need(location.origin === 'https://pan.baidu.com' && source.path.startsWith('/apps/教育分析学习/') && new URL(location.href).searchParams.get('path') === source.path, 'SOURCE_CHANGED');
    pathGuard();
    const hash = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
    const account = 'baidu-'+(await hash(new TextEncoder().encode('worldanalysis-account:'+String(window.locals?.uk||'')))).slice(0,24);
    pathGuard(); need(account === source.account_ref, 'ACCOUNT_MISMATCH');
    // The ad and file players overlap with identical rectangles on this site.
    // Use the observed file-player role, never geometry alone or the ad decoder.
    const videos = [...document.querySelectorAll('video')].filter(v=>v.closest('.vp-file-video-container') && v.getBoundingClientRect().width > 0 && v.getBoundingClientRect().height > 0);
    need(videos.length === 1, 'VISIBLE_SOURCE_PLAYER_REQUIRED'); const video=videos[0];
    const identity = () => {
      pathGuard();
      const player=video.closest('.video-js')?.player, tracks=player?.textTracks?.();
      let found=false;
      for(let i=0;tracks&&i<tracks.length;i++) {
        const t=tracks[i]; if(t.kind!=='metadata'||!t.cues)continue;
        for(let k=0;k<t.cues.length;k++) {
          const cue=t.cues[k]; if(cue.startTime>video.currentTime+0.1||cue.endTime<video.currentTime-0.1)continue;
          try { const value=JSON.parse(cue.text); if(typeof value.uri!=='string')continue; const u=new URL(value.uri);
            if(u.searchParams.get('fsid')===source.file_id && u.pathname.includes(config.version_token+'_'))found=true;
          } catch { /* Ignore unrelated metadata without exporting it. */ }
        }
      }
      need(found,'PLAYER_FILE_ID_AND_VERSION_NOT_CONFIRMED');
      return {file_id_match:true,version_token_match:true};
    };
    try {
      video.pause();
      if(video.readyState<2||!video.videoWidth) {
        window.__waCaptureAllowPlay=true;
        const ready=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('SOURCE_DECODE_TIMEOUT')),20000);const check=()=>{if(video.readyState>=2&&video.videoWidth){clearTimeout(timer);video.removeEventListener('loadeddata',check);video.removeEventListener('timeupdate',check);resolve(true);}};video.addEventListener('loadeddata',check);video.addEventListener('timeupdate',check);check();});
        await video.play(); await ready; pathGuard(); video.pause(); window.__waCaptureAllowPlay=false;
      }
      identity();
      const actual=await new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>reject(Error('SOURCE_FRAME_TIMEOUT')),20000);
        const cb=(clock,m)=>{try {pathGuard();if(video.seeking || Math.abs(m.mediaTime*1000-config.requested_ms)>80){video.requestVideoFrameCallback(cb);return;}clearTimeout(timer);resolve({mediaTime:m.mediaTime,presentedFrames:m.presentedFrames,width:m.width,height:m.height});}catch(e){clearTimeout(timer);reject(e);}};
        video.requestVideoFrameCallback(cb); video.currentTime=config.requested_ms/1000;
      });
      video.pause();const proof=identity();
      const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
      need(canvas.width>0&&canvas.height>0&&canvas.width*canvas.height<=16777216,'FRAME_PIXEL_BOUND');
      canvas.getContext('2d').drawImage(video,0,0);
      const base64=canvas.toDataURL('image/jpeg',0.94).split(',')[1];
      const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));need(bytes.length<=4*1024*1024,'FRAME_BYTE_BOUND');
      const sha256=await hash(bytes);identity();
      return {schema:'worldanalysis.browser-source-frame.v1',source,helper_sha256:config.helper_sha256,requested_ms:config.requested_ms,
        frame_callback:actual,source_identity_check:proof,relative_path:config.relative_path,sha256,bytes:bytes.length,width:canvas.width,height:canvas.height,
        format:'jpeg',observed_at:new Date().toISOString(),playback_duration_ms:video.duration*1000,locator:'requestVideoFrameCallback',base64};
    } finally { video.pause();window.__waCaptureAllowPlay=false; }
  };
  return {installed:true,namespace:'WASourceFrameCapture'};
})()
