// Official upstream treats `keywords` as a filename; inline text uses keywordsBuf.
const assert = require('assert');
const {rideKwsCreate, RIDE_KWS_FILES} = require('../static/kws_worker.js');
let config;
const Module = {FS_createPath() {}, FS_createDataFile() {}};
rideKwsCreate(Module, (_module, c) => {
  config = c;
  return {createStream() { return {}; }};
}, Object.fromEntries(RIDE_KWS_FILES.map(n => [n, new Uint8Array()])), ' HH EY1 @HEY\n');
assert.strictEqual(config.keywords, '');
assert.strictEqual(config.keywordsBuf, 'HH EY1 @HEY');
assert.strictEqual(config.keywordsBufSize, Buffer.byteLength('HH EY1 @HEY'));
console.log(JSON.stringify({checks: 3, failures: []}));
