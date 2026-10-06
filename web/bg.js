// Animated deep-navy reactor background (WebGL1 domain-warped fbm).
// Renders at reduced resolution, ~30 fps cap, pauses when hidden.
(function () {
  const canvas = document.getElementById('bg');
  const gl = canvas.getContext('webgl', { antialias: false, alpha: false, depth: false, stencil: false,
                                          preserveDrawingBuffer: !!window.BG_PRESERVE, powerPreference: 'low-power' });
  const BG = { fps: 30, idleFps: 8, idleFreezeMs: 15000, paused: false, focused: true, blurredAt: 0, speed: 1.0, energy: 0.0 };
  window.BG = BG;
  if (!gl) { document.body.classList.add('no-webgl'); return; }

  const vs = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
  const fs = `
precision mediump float;
uniform vec2 R; uniform float T; uniform float E;
float h(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
float n(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.-2.*f);
  return mix(mix(h(i),h(i+vec2(1.,0.)),u.x),mix(h(i+vec2(0.,1.)),h(i+vec2(1.,1.)),u.x),u.y);}
float fbm(vec2 p){float v=0.,a=.5;mat2 m=mat2(1.6,1.2,-1.2,1.6);
  for(int i=0;i<5;i++){v+=a*n(p);p=m*p;a*=.5;}return v;}
void main(){
  vec2 uv=gl_FragCoord.xy/R;
  vec2 p=(gl_FragCoord.xy-.5*R)/R.y*0.9;
  float t=T*.022;
  vec2 q=vec2(fbm(p+vec2(0.,0.)+t*.6),fbm(p+vec2(5.2,1.3)-t*.5));
  vec2 r=vec2(fbm(p+2.8*q+vec2(1.7,9.2)+t*.9),fbm(p+2.8*q+vec2(8.3,2.8)-t*.7));
  float f=fbm(p+3.2*r);
  float c=smoothstep(.2,.9,f*1.05+.12*length(q));
  float vein=abs(sin((f*6.0+r.x*2.4)*1.15));
  vein=smoothstep(0.02,.22,vein);
  vec3 deep=vec3(0.04,0.07,0.14);
  vec3 mid=vec3(0.10,0.22,0.42);
  vec3 glow=vec3(0.45,0.78,1.0);
  vec3 amber=vec3(0.95,0.72,0.42);
  vec3 col=mix(deep, mid, c);
  col=mix(col, glow, (1.0-vein)*0.28*smoothstep(.35,.85,f));
  float core=smoothstep(.55,.95,f)*smoothstep(1.15,.45,length(uv-vec2(.62,.58)));
  col=mix(col, amber, core*.35);
  col+=glow*(.08*E);
  col+= (h(gl_FragCoord.xy+T)-.5)*.015;
  gl_FragColor=vec4(col,1.);
}`;
  function sh(type, src) {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { console.error(gl.getShaderInfoLog(s)); }
    return s;
  }
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, vs));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { document.body.classList.add('no-webgl'); return; }
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const uR = gl.getUniformLocation(prog, 'R'), uT = gl.getUniformLocation(prog, 'T'), uE = gl.getUniformLocation(prog, 'E');

  let w = 0, h = 0;
  function resize() {
    const cw = window.innerWidth, ch = window.innerHeight;
    const scale = Math.min(0.5, 640 / Math.max(1, cw));
    const nw = Math.max(64, Math.round(cw * scale)), nh = Math.max(64, Math.round(ch * scale));
    if (nw !== w || nh !== h) { w = nw; h = nh; canvas.width = w; canvas.height = h; gl.viewport(0, 0, w, h); }
  }
  window.addEventListener('resize', resize);
  resize();

  const seed = (Date.now() / 1000) % 1000;
  let simT = 40 + (seed % 60), last = performance.now(), acc = 0, raf = 0, energy = 0;
  function frame(now) {
    raf = 0;
    if (BG.paused || document.hidden) { return; }
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    if (!BG.focused && BG.energy === 0 && now - BG.blurredAt > BG.idleFreezeMs) { return; }
    const target = 1 / (BG.focused || BG.energy > 0 ? BG.fps : BG.idleFps);
    acc += dt;
    if (acc >= target) {
      energy += (BG.energy - energy) * Math.min(1, acc * 3);
      simT += acc * BG.speed * (1 + energy * 1.5);
      acc = 0;
      gl.uniform2f(uR, w, h); gl.uniform1f(uT, simT); gl.uniform1f(uE, energy);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf && !BG.paused && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); } }
  BG.setPaused = (p) => { BG.paused = !!p; kick(); };
  BG.kick = kick;
  document.addEventListener('visibilitychange', kick);
  window.addEventListener('focus', () => { BG.focused = true; kick(); });
  window.addEventListener('blur', () => { BG.focused = false; BG.blurredAt = performance.now(); });
  BG.focused = document.hasFocus(); BG.blurredAt = performance.now();
  gl.uniform2f(uR, w, h); gl.uniform1f(uT, simT); gl.uniform1f(uE, 0); gl.drawArrays(gl.TRIANGLES, 0, 3);
  kick();
})();
