// Mic -> PCM16 mono 16 kHz in 20 ms frames (320 samples), each posted with its RMS level in dBFS. The VAD gate
// (gate.js) runs on these levels on the main thread; nothing is sent anywhere from here.
class Pcm16Writer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 16000;  // input samples per output sample
    this.pos = 0;                    // fractional read position into the current input block
    this.acc = 0; this.n = 0;        // box filter (average) over each output sample's input span
    this.out = new Int16Array(320); this.i = 0; this.sq = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let k = 0; k < ch.length; k++) {
      this.acc += ch[k]; this.n++;
      this.pos += 1;
      if (this.pos >= this.step) {
        this.pos -= this.step;
        const v = Math.max(-1, Math.min(1, this.acc / this.n));
        this.acc = 0; this.n = 0;
        this.sq += v * v;
        this.out[this.i++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.i === this.out.length) {
          const db = 10 * Math.log10(this.sq / this.out.length + 1e-12);
          this.port.postMessage({pcm: this.out.buffer, db}, [this.out.buffer]);
          this.out = new Int16Array(320); this.i = 0; this.sq = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm16-writer', Pcm16Writer);
