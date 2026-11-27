// Synthetic browser contract only: no network, real JPEG decoding, or live-site acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createHash, webcrypto} from 'node:crypto';

const code = fs.readFileSync(new URL('./browser-source-frame.js', import.meta.url), 'utf8');
const helperSha256 = createHash('sha256').update(code).digest('hex');
const sourcePath = '/apps/教育分析学习/synthetic-test.mp4';
const fileId = '123456789';
const versionToken = 'SyntheticVersion0001';
const accountUk = 'synthetic-account-id';
const accountRef = 'baidu-' + createHash('sha256').update('worldanalysis-account:' + accountUk).digest('hex').slice(0, 24);
const canvasBytes = Buffer.from('synthetic-canvas-bytes-not-a-jpeg');
const metadataUri = (fsid = fileId, version = versionToken) =>
  `https://media.invalid/${version}_segment.ts?fsid=${fsid}&sign=SYNTHETIC_NOT_A_CREDENTIAL`;
const cue = (uri = metadataUri(), startTime = 0, endTime = 500) =>
  ({startTime, endTime, text: JSON.stringify({uri, playlist: 'https://media.invalid/synthetic-playlist.m3u8'})});

function fixture({cues = [cue()]} = {}) {
  const timers = new Map(), draws = [];
  let timerId = 0, frameRegistered;
  const frameReady = new Promise(resolve => { frameRegistered = resolve; });
  const rect = {x: 0, y: 0, top: 0, left: 0, right: 888, bottom: 568, width: 888, height: 568};
  const tracks = [{kind: 'metadata', cues}];
  function fakeVideo(isMain) {
    const fileAncestor = {}, adAncestor = {};
    const playerAncestor = {player: {textTracks: () => isMain ? tracks : []}};
    return {
      readyState: isMain ? 4 : 0, videoWidth: isMain ? 1920 : 0, videoHeight: isMain ? 1080 : 0,
      duration: 500, paused: true, seeking: false, _time: isMain ? 392 : 0,
      currentSrc: 'blob:https://pan.baidu.com/synthetic-video',
      frames: [], frameRequests: 0, pauseCalls: 0, playCalls: 0, seekWrites: [],
      get currentTime() { return this._time; },
      set currentTime(value) { this._time = value; this.seeking = true; this.seekWrites.push(value); },
      getBoundingClientRect() { return {...rect}; },
      closest(selector) {
        if (selector === '.vp-file-video-container') return isMain ? fileAncestor : null;
        if (selector === '.vp-ad-video-container') return isMain ? null : adAncestor;
        return selector === '.video-js' ? playerAncestor : null;
      },
      pause() { this.paused = true; this.pauseCalls++; },
      play() { this.playCalls++; throw Error('UNEXPECTED_SYNTHETIC_PLAYER_PLAY'); },
      requestVideoFrameCallback(callback) { this.frames.push(callback); frameRegistered(); return ++this.frameRequests; },
      addEventListener() {}, removeEventListener() {},
    };
  }
  const ad = fakeVideo(false), main = fakeVideo(true);
  const document = {
    querySelectorAll(selector) { assert.equal(selector, 'video'); return [ad, main]; },
    createElement(tag) {
      assert.equal(tag, 'canvas');
      return {width: 0, height: 0,
        getContext(kind) { assert.equal(kind, '2d'); return {drawImage: (...args) => draws.push(args)}; },
        toDataURL(type, quality) {
          assert.equal(type, 'image/jpeg'); assert.equal(quality, 0.94);
          return 'data:image/jpeg;base64,' + canvasBytes.toString('base64');
        },
      };
    },
  };
  const context = vm.createContext({
    window: {locals: {uk: accountUk}}, document,
    location: {origin: 'https://pan.baidu.com', href: 'https://pan.baidu.com/pfile/video?path=' + encodeURIComponent(sourcePath)},
    URL, TextEncoder, crypto: webcrypto, Uint8Array, atob: value => Buffer.from(value, 'base64').toString('binary'),
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, {callback, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  assert.equal(vm.runInContext(code, context).installed, true);
  const config = {
    source: {file_id: fileId, source_revision: 'synthetic-source-revision-001', path: sourcePath, account_ref: accountRef},
    helper_sha256: helperSha256, version_token: versionToken, requested_ms: 2000, relative_path: 'synthetic/frame-002000.jpg',
  };
  return {context, config, main, ad, tracks, draws, timers, frameReady,
    capture() { return context.window.WASourceFrameCapture(config); },
    emit(mediaTime, seeking = false) {
      main.seeking = seeking; assert.equal(main.frames.length, 1, 'exactly one main rVFC must be pending');
      main.frames.shift()(0, {mediaTime, presentedFrames: 19, width: 1920, height: 1080});
    },
  };
}

async function start(f) {
  const pending = f.capture();
  // Propagate startup rejection instead of hanging while waiting for an unregistered rVFC.
  await Promise.race([f.frameReady, pending.then(() => assert.fail('capture resolved before a frame'))]);
  return {pending};
}
async function finish(f) {
  const {pending} = await start(f); f.emit(2); return pending;
}
function assertStopped(f) {
  assert.equal(f.main.paused, true); assert.equal(f.context.window.__waCaptureAllowPlay, false);
  assert.equal(f.timers.size, 0);
}

test('identical 888x568 rectangles select only the observed main file ancestor, not the ad decoder', async () => {
  const f = fixture();
  assert.deepEqual(f.main.getBoundingClientRect(), f.ad.getBoundingClientRect());
  assert.equal(f.main.readyState, 4); assert.equal(f.main.videoWidth, 1920); assert.equal(f.main.videoHeight, 1080);
  assert.equal(f.tracks.filter(track => track.kind === 'metadata').length, 1);
  assert.equal(f.ad.readyState, 0); assert.equal(f.ad.videoWidth, 0);
  const result = await finish(f);
  assert.equal(f.ad.playCalls, 0); assert.equal(f.ad.pauseCalls, 0); assert.equal(f.ad.frameRequests, 0);
  assert.deepEqual(f.main.seekWrites, [2]); assert.equal(f.draws.length, 1);
  assert.strictEqual(f.draws[0][0], f.main);
  assert.equal(result.width, 1920); assert.equal(result.height, 1080);
  assert.equal(result.source_identity_check.file_id_match, true);
  assert.equal(result.source_identity_check.version_token_match, true);
  assertStopped(f);
});

test('backward seek from 392s to 2s ignores stale rVFC and waits for a real non-seeking target frame', async () => {
  const f = fixture(); let settled = false;
  assert.equal(f.main.currentTime, 392);
  const {pending} = await start(f); pending.then(() => { settled = true; }, () => { settled = true; });
  assert.equal(f.main.currentTime, 2);
  for (const [time, seeking] of [[392, false], [2, true], [2.25, false]]) {
    f.emit(time, seeking); await Promise.resolve();
    assert.equal(settled, false); assert.equal(f.draws.length, 0);
    assert.equal(f.main.frames.length, 1); assert.equal(f.timers.size, 1);
  }
  f.emit(2); const result = await pending;
  assert.equal(result.requested_ms, 2000); assert.equal(result.frame_callback.mediaTime, 2);
  assert.equal(result.frame_callback.presentedFrames, 19); assert.equal(f.main.frameRequests, 4);
  assertStopped(f);
});

test('one active metadata.uri must prove both file and version, including again after seek', async () => {
  const mismatches = [
    [cue(metadataUri('987654321'))],
    [cue(metadataUri(fileId, 'DifferentVersion0001'))],
    [cue(metadataUri('987654321')), cue(metadataUri(fileId, 'DifferentVersion0001')), cue(metadataUri(), 1000, 1001)],
  ];
  for (const cues of mismatches) {
    const f = fixture({cues});
    await assert.rejects(f.capture(), /PLAYER_FILE_ID_AND_VERSION_NOT_CONFIRMED/);
    assert.equal(f.main.frameRequests, 0); assert.equal(f.draws.length, 0); assertStopped(f);
  }
  const f = fixture({cues: [cue(metadataUri(), 300, 400), cue(metadataUri('987654321'), 0, 10)]});
  const {pending} = await start(f); f.emit(2);
  await assert.rejects(pending, /PLAYER_FILE_ID_AND_VERSION_NOT_CONFIRMED/);
  assert.equal(f.draws.length, 0); assertStopped(f);
});

test('page path/origin and account guards fail closed, including path changes during rVFC wait', async () => {
  for (const change of [
    f => { f.context.location.href = 'https://pan.baidu.com/pfile/video?path=' + encodeURIComponent(sourcePath + '.other'); },
    f => { f.context.location.origin = 'https://other.invalid'; },
    f => { f.config.source.path = '/not-authorized/synthetic-test.mp4'; },
  ]) {
    const f = fixture(); change(f);
    await assert.rejects(f.capture(), /SOURCE_CHANGED/);
    assert.equal(f.main.frameRequests, 0); assert.equal(f.draws.length, 0);
  }
  const wrongAccount = fixture(); wrongAccount.context.window.locals.uk = 'another-synthetic-account';
  await assert.rejects(wrongAccount.capture(), /ACCOUNT_MISMATCH/);
  assert.equal(wrongAccount.main.frameRequests, 0); assert.equal(wrongAccount.draws.length, 0);
  const f = fixture(); const {pending} = await start(f);
  f.context.location.href = 'https://pan.baidu.com/pfile/video?path=' + encodeURIComponent(sourcePath + '.changed');
  f.emit(2); await assert.rejects(pending, /SOURCE_CHANGED/);
  assert.equal(f.draws.length, 0); assertStopped(f);
});

test('result carries bounded synthetic canvas bytes and same-source proof, never raw URI or playlist/signatures', async () => {
  const f = fixture(), result = await finish(f);
  assert.equal(result.schema, 'worldanalysis.browser-source-frame.v1');
  assert.deepEqual(JSON.parse(JSON.stringify(result.source)), f.config.source);
  assert.equal(Object.isFrozen(result.source), true); assert.notStrictEqual(result.source, f.config.source);
  assert.equal(result.helper_sha256, helperSha256); assert.equal(result.relative_path, f.config.relative_path);
  assert.equal(result.sha256, createHash('sha256').update(canvasBytes).digest('hex'));
  assert.equal(result.bytes, canvasBytes.length); assert.equal(result.base64, canvasBytes.toString('base64'));
  assert.equal(result.format, 'jpeg'); assert.equal(result.locator, 'requestVideoFrameCallback');
  const output = JSON.stringify(result);
  for (const forbidden of [metadataUri(), 'SYNTHETIC_NOT_A_CREDENTIAL', 'media.invalid', versionToken, 'rawURI', 'playlist', '"uri"'])
    assert.equal(output.includes(forbidden), false, `output must not expose ${forbidden}`);
  assertStopped(f);
});
