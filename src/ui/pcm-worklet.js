class MicrophonePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(2560);
    this.used = 0;
    this.port.onmessage = event => {
      if (event.data === 'flush') {
        this.flush();
        this.port.postMessage({ type: 'flushed' });
      }
    };
  }
  flush() {
    if (!this.used) return;
    const buffer = new ArrayBuffer(this.used * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < this.used; i++) view.setInt16(i * 2, this.samples[i], true);
    this.port.postMessage({ type: 'audio', buffer }, [buffer]);
    this.used = 0;
  }
  process(inputs) {
    for (const value of inputs[0]?.[0] || []) {
      const sample = Math.max(-1, Math.min(1, value));
      this.samples[this.used++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      if (this.used === this.samples.length) this.flush();
    }
    return true;
  }
}
registerProcessor('microphone-pcm', MicrophonePCM);
