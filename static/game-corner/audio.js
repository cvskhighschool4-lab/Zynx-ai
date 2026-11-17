export class AudioSynth{
 constructor(){this.ctx=null;this.master=null;this.osc=null;this.gain=null;this.enabled=true}
 start(){if(this.ctx)return;const C=window.AudioContext||window.webkitAudioContext;if(!C)return;this.ctx=new C();this.master=this.ctx.createGain();this.master.gain.value=.16;this.master.connect(this.ctx.destination)}
 tone(freq=440,duration=.12,type="sine",volume=.22){if(!this.enabled)return;this.start();if(!this.ctx)return;const o=this.ctx.createOscillator(),g=this.ctx.createGain(),t=this.ctx.currentTime;o.type=type;o.frequency.setValueAtTime(freq,t);g.gain.setValueAtTime(.0001,t);g.gain.exponentialRampToValueAtTime(volume,t+.012);g.gain.exponentialRampToValueAtTime(.0001,t+duration);o.connect(g);g.connect(this.master);o.start(t);o.stop(t+duration+.03)}
 boost(){this.tone(170,.32,"sawtooth",.12);setTimeout(()=>this.tone(330,.18,"triangle",.1),65)}
 score(){this.tone(640,.09,"sine",.18);setTimeout(()=>this.tone(920,.14,"sine",.14),80)}
 bump(){this.tone(95,.12,"triangle",.18)}
}
export const audio=new AudioSynth();
