import{a as e}from"./6c87f68371b28954707ebb92afee7ccf-Cyuzqnbw.js";import{Gt as t,Ut as n}from"./c0bc1e08f9743b2d50d5f1607503bf4e-mEcGrjeo.js";import{Er as r,ct as i,ni as a}from"./bdf49c3c3882102fc017ffb661108c63-DZvWdWoT.js";import{tn as o}from"./1bc04b5291c26a46d918139138b992d2-LE4mTIM4.js";var s=`#version 300 es
precision mediump float;

layout(location = 0) in vec4 a_position;

uniform vec2 u_resolution;
uniform float u_pixelRatio;
uniform float u_imageAspectRatio;
uniform float u_originX;
uniform float u_originY;
uniform float u_worldWidth;
uniform float u_worldHeight;
uniform float u_fit;
uniform float u_scale;
uniform float u_rotation;
uniform float u_offsetX;
uniform float u_offsetY;

out vec2 v_objectUV;
out vec2 v_objectBoxSize;
out vec2 v_responsiveUV;
out vec2 v_responsiveBoxGivenSize;
out vec2 v_patternUV;
out vec2 v_patternBoxSize;
out vec2 v_imageUV;

vec3 getBoxSize(float boxRatio, vec2 givenBoxSize) {
  vec2 box = vec2(0.);
  // fit = none
  box.x = boxRatio * min(givenBoxSize.x / boxRatio, givenBoxSize.y);
  float noFitBoxWidth = box.x;
  if (u_fit == 1.) { // fit = contain
    box.x = boxRatio * min(u_resolution.x / boxRatio, u_resolution.y);
  } else if (u_fit == 2.) { // fit = cover
    box.x = boxRatio * max(u_resolution.x / boxRatio, u_resolution.y);
  }
  box.y = box.x / boxRatio;
  return vec3(box, noFitBoxWidth);
}

void main() {
  gl_Position = a_position;

  vec2 uv = gl_Position.xy * .5;
  vec2 boxOrigin = vec2(.5 - u_originX, u_originY - .5);
  vec2 givenBoxSize = vec2(u_worldWidth, u_worldHeight);
  givenBoxSize = max(givenBoxSize, vec2(1.)) * u_pixelRatio;
  float r = u_rotation * 3.14159265358979323846 / 180.;
  mat2 graphicRotation = mat2(cos(r), sin(r), -sin(r), cos(r));
  vec2 graphicOffset = vec2(-u_offsetX, u_offsetY);


  // ===================================================

  float fixedRatio = 1.;
  vec2 fixedRatioBoxGivenSize = vec2(
  (u_worldWidth == 0.) ? u_resolution.x : givenBoxSize.x,
  (u_worldHeight == 0.) ? u_resolution.y : givenBoxSize.y
  );

  v_objectBoxSize = getBoxSize(fixedRatio, fixedRatioBoxGivenSize).xy;
  vec2 objectWorldScale = u_resolution.xy / v_objectBoxSize;

  v_objectUV = uv;
  v_objectUV *= objectWorldScale;
  v_objectUV += boxOrigin * (objectWorldScale - 1.);
  v_objectUV += graphicOffset;
  v_objectUV /= u_scale;
  v_objectUV = graphicRotation * v_objectUV;

  // ===================================================

  v_responsiveBoxGivenSize = vec2(
  (u_worldWidth == 0.) ? u_resolution.x : givenBoxSize.x,
  (u_worldHeight == 0.) ? u_resolution.y : givenBoxSize.y
  );
  float responsiveRatio = v_responsiveBoxGivenSize.x / v_responsiveBoxGivenSize.y;
  vec2 responsiveBoxSize = getBoxSize(responsiveRatio, v_responsiveBoxGivenSize).xy;
  vec2 responsiveBoxScale = u_resolution.xy / responsiveBoxSize;

  #ifdef ADD_HELPERS
  v_responsiveHelperBox = uv;
  v_responsiveHelperBox *= responsiveBoxScale;
  v_responsiveHelperBox += boxOrigin * (responsiveBoxScale - 1.);
  #endif

  v_responsiveUV = uv;
  v_responsiveUV *= responsiveBoxScale;
  v_responsiveUV += boxOrigin * (responsiveBoxScale - 1.);
  v_responsiveUV += graphicOffset;
  v_responsiveUV /= u_scale;
  v_responsiveUV.x *= responsiveRatio;
  v_responsiveUV = graphicRotation * v_responsiveUV;
  v_responsiveUV.x /= responsiveRatio;

  // ===================================================

  float patternBoxRatio = givenBoxSize.x / givenBoxSize.y;
  vec2 patternBoxGivenSize = vec2(
  (u_worldWidth == 0.) ? u_resolution.x : givenBoxSize.x,
  (u_worldHeight == 0.) ? u_resolution.y : givenBoxSize.y
  );
  patternBoxRatio = patternBoxGivenSize.x / patternBoxGivenSize.y;

  vec3 boxSizeData = getBoxSize(patternBoxRatio, patternBoxGivenSize);
  v_patternBoxSize = boxSizeData.xy;
  float patternBoxNoFitBoxWidth = boxSizeData.z;
  vec2 patternBoxScale = u_resolution.xy / v_patternBoxSize;

  v_patternUV = uv;
  v_patternUV += graphicOffset / patternBoxScale;
  v_patternUV += boxOrigin;
  v_patternUV -= boxOrigin / patternBoxScale;
  v_patternUV *= u_resolution.xy;
  v_patternUV /= u_pixelRatio;
  if (u_fit > 0.) {
    v_patternUV *= (patternBoxNoFitBoxWidth / v_patternBoxSize.x);
  }
  v_patternUV /= u_scale;
  v_patternUV = graphicRotation * v_patternUV;
  v_patternUV += boxOrigin / patternBoxScale;
  v_patternUV -= boxOrigin;
  // x100 is a default multiplier between vertex and fragmant shaders
  // we use it to avoid UV presision issues
  v_patternUV *= .01;

  // ===================================================

  vec2 imageBoxSize;
  if (u_fit == 1.) { // contain
    imageBoxSize.x = min(u_resolution.x / u_imageAspectRatio, u_resolution.y) * u_imageAspectRatio;
  } else if (u_fit == 2.) { // cover
    imageBoxSize.x = max(u_resolution.x / u_imageAspectRatio, u_resolution.y) * u_imageAspectRatio;
  } else {
    imageBoxSize.x = min(10.0, 10.0 / u_imageAspectRatio * u_imageAspectRatio);
  }
  imageBoxSize.y = imageBoxSize.x / u_imageAspectRatio;
  vec2 imageBoxScale = u_resolution.xy / imageBoxSize;

  v_imageUV = uv;
  v_imageUV *= imageBoxScale;
  v_imageUV += boxOrigin * (imageBoxScale - 1.);
  v_imageUV += graphicOffset;
  v_imageUV /= u_scale;
  v_imageUV.x *= u_imageAspectRatio;
  v_imageUV = graphicRotation * v_imageUV;
  v_imageUV.x /= u_imageAspectRatio;

  v_imageUV += .5;
  v_imageUV.y = 1. - v_imageUV.y;
}`,c=1920*1080*4,l=class{parentElement;canvasElement;gl;program=null;uniformLocations={};fragmentShader;rafId=null;lastRenderTime=0;currentFrame=0;speed=0;currentSpeed=0;providedUniforms;mipmaps=[];hasBeenDisposed=!1;resolutionChanged=!0;textures=new Map;minPixelRatio;maxPixelCount;isSafari=p();uniformCache={};textureUnitMap=new Map;ownerDocument;constructor(e,t,n,r,i=0,a=0,o=2,s=c,l=[]){if(e?.nodeType===1)this.parentElement=e;else throw Error(`Paper Shaders: parent element must be an HTMLElement`);if(this.ownerDocument=e.ownerDocument,!this.ownerDocument.querySelector(`style[data-paper-shader]`)){let e=this.ownerDocument.createElement(`style`);e.innerHTML=f,e.setAttribute(`data-paper-shader`,``),this.ownerDocument.head.prepend(e)}let u=this.ownerDocument.createElement(`canvas`);this.canvasElement=u,this.parentElement.prepend(u),this.fragmentShader=t,this.providedUniforms=n,this.mipmaps=l,this.currentFrame=a,this.minPixelRatio=o,this.maxPixelCount=s;let d=u.getContext(`webgl2`,r);if(!d)throw Error(`Paper Shaders: WebGL is not supported in this browser`);this.gl=d,this.initProgram(),this.setupPositionAttribute(),this.setupUniforms(),this.setUniformValues(this.providedUniforms),this.setupResizeObserver(),visualViewport?.addEventListener(`resize`,this.handleVisualViewportChange),this.setSpeed(i),this.parentElement.setAttribute(`data-paper-shader`,``),this.parentElement.paperShaderMount=this,this.ownerDocument.addEventListener(`visibilitychange`,this.handleDocumentVisibilityChange)}initProgram=()=>{let e=d(this.gl,s,this.fragmentShader);e&&(this.program=e)};setupPositionAttribute=()=>{let e=this.gl.getAttribLocation(this.program,`a_position`),t=this.gl.createBuffer();this.gl.bindBuffer(this.gl.ARRAY_BUFFER,t),this.gl.bufferData(this.gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),this.gl.STATIC_DRAW),this.gl.enableVertexAttribArray(e),this.gl.vertexAttribPointer(e,2,this.gl.FLOAT,!1,0,0)};setupUniforms=()=>{let e={u_time:this.gl.getUniformLocation(this.program,`u_time`),u_pixelRatio:this.gl.getUniformLocation(this.program,`u_pixelRatio`),u_resolution:this.gl.getUniformLocation(this.program,`u_resolution`)};Object.entries(this.providedUniforms).forEach(([t,n])=>{if(e[t]=this.gl.getUniformLocation(this.program,t),n instanceof HTMLImageElement){let n=`${t}AspectRatio`;e[n]=this.gl.getUniformLocation(this.program,n)}}),this.uniformLocations=e};renderScale=1;parentWidth=0;parentHeight=0;parentDevicePixelWidth=0;parentDevicePixelHeight=0;devicePixelsSupported=!1;resizeObserver=null;setupResizeObserver=()=>{this.resizeObserver=new ResizeObserver(([e])=>{if(e?.borderBoxSize[0]){let t=e.devicePixelContentBoxSize?.[0];t!==void 0&&(this.devicePixelsSupported=!0,this.parentDevicePixelWidth=t.inlineSize,this.parentDevicePixelHeight=t.blockSize),this.parentWidth=e.borderBoxSize[0].inlineSize,this.parentHeight=e.borderBoxSize[0].blockSize}this.handleResize()}),this.resizeObserver.observe(this.parentElement)};handleVisualViewportChange=()=>{this.resizeObserver?.disconnect(),this.setupResizeObserver()};handleResize=()=>{let e=0,t=0,n=Math.max(1,window.devicePixelRatio),r=visualViewport?.scale??1;if(this.devicePixelsSupported){let i=Math.max(1,this.minPixelRatio/n);e=this.parentDevicePixelWidth*i*r,t=this.parentDevicePixelHeight*i*r}else{let i=Math.max(n,this.minPixelRatio)*r;if(this.isSafari){let e=m(this.ownerDocument);i*=Math.max(1,e)}e=Math.round(this.parentWidth)*i,t=Math.round(this.parentHeight)*i}let i=Math.sqrt(this.maxPixelCount)/Math.sqrt(e*t),a=Math.min(1,i),o=Math.round(e*a),s=Math.round(t*a),c=o/Math.round(this.parentWidth);(this.canvasElement.width!==o||this.canvasElement.height!==s||this.renderScale!==c)&&(this.renderScale=c,this.canvasElement.width=o,this.canvasElement.height=s,this.resolutionChanged=!0,this.gl.viewport(0,0,this.gl.canvas.width,this.gl.canvas.height),this.render(performance.now()))};render=e=>{if(this.hasBeenDisposed)return;if(this.program===null){console.warn(`Tried to render before program or gl was initialized`);return}let t=e-this.lastRenderTime;this.lastRenderTime=e,this.currentSpeed!==0&&(this.currentFrame+=t*this.currentSpeed),this.gl.clear(this.gl.COLOR_BUFFER_BIT),this.gl.useProgram(this.program),this.gl.uniform1f(this.uniformLocations.u_time,this.currentFrame*.001),this.resolutionChanged&&=(this.gl.uniform2f(this.uniformLocations.u_resolution,this.gl.canvas.width,this.gl.canvas.height),this.gl.uniform1f(this.uniformLocations.u_pixelRatio,this.renderScale),!1),this.gl.drawArrays(this.gl.TRIANGLES,0,6),this.currentSpeed===0?this.rafId=null:this.requestRender()};requestRender=()=>{this.rafId!==null&&cancelAnimationFrame(this.rafId),this.rafId=requestAnimationFrame(this.render)};setTextureUniform=(e,t)=>{if(!t.complete||t.naturalWidth===0)throw Error(`Paper Shaders: image for uniform ${e} must be fully loaded`);let n=this.textures.get(e);n&&this.gl.deleteTexture(n),this.textureUnitMap.has(e)||this.textureUnitMap.set(e,this.textureUnitMap.size);let r=this.textureUnitMap.get(e);this.gl.activeTexture(this.gl.TEXTURE0+r);let i=this.gl.createTexture();this.gl.bindTexture(this.gl.TEXTURE_2D,i),this.gl.texParameteri(this.gl.TEXTURE_2D,this.gl.TEXTURE_WRAP_S,this.gl.CLAMP_TO_EDGE),this.gl.texParameteri(this.gl.TEXTURE_2D,this.gl.TEXTURE_WRAP_T,this.gl.CLAMP_TO_EDGE),this.gl.texParameteri(this.gl.TEXTURE_2D,this.gl.TEXTURE_MIN_FILTER,this.gl.LINEAR),this.gl.texParameteri(this.gl.TEXTURE_2D,this.gl.TEXTURE_MAG_FILTER,this.gl.LINEAR),this.gl.texImage2D(this.gl.TEXTURE_2D,0,this.gl.RGBA,this.gl.RGBA,this.gl.UNSIGNED_BYTE,t),this.mipmaps.includes(e)&&(this.gl.generateMipmap(this.gl.TEXTURE_2D),this.gl.texParameteri(this.gl.TEXTURE_2D,this.gl.TEXTURE_MIN_FILTER,this.gl.LINEAR_MIPMAP_LINEAR));let a=this.gl.getError();if(a!==this.gl.NO_ERROR||i===null){console.error(`Paper Shaders: WebGL error when uploading texture:`,a);return}this.textures.set(e,i);let o=this.uniformLocations[e];if(o){this.gl.uniform1i(o,r);let n=`${e}AspectRatio`,i=this.uniformLocations[n];if(i){let e=t.naturalWidth/t.naturalHeight;this.gl.uniform1f(i,e)}}};areUniformValuesEqual=(e,t)=>e===t?!0:Array.isArray(e)&&Array.isArray(t)&&e.length===t.length?e.every((e,n)=>this.areUniformValuesEqual(e,t[n])):!1;setUniformValues=e=>{this.gl.useProgram(this.program),Object.entries(e).forEach(([e,t])=>{let n=t;if(t instanceof HTMLImageElement&&(n=`${t.src.slice(0,200)}|${t.naturalWidth}x${t.naturalHeight}`),this.areUniformValuesEqual(this.uniformCache[e],n))return;this.uniformCache[e]=n;let r=this.uniformLocations[e];if(!r){console.warn(`Uniform location for ${e} not found`);return}if(t instanceof HTMLImageElement)this.setTextureUniform(e,t);else if(Array.isArray(t)){let n=null,i=null;if(t[0]!==void 0&&Array.isArray(t[0])){let r=t[0].length;if(t.every(e=>e.length===r))n=t.flat(),i=r;else{console.warn(`All child arrays must be the same length for ${e}`);return}}else n=t,i=n.length;switch(i){case 2:this.gl.uniform2fv(r,n);break;case 3:this.gl.uniform3fv(r,n);break;case 4:this.gl.uniform4fv(r,n);break;case 9:this.gl.uniformMatrix3fv(r,!1,n);break;case 16:this.gl.uniformMatrix4fv(r,!1,n);break;default:console.warn(`Unsupported uniform array length: ${i}`)}}else typeof t==`number`?this.gl.uniform1f(r,t):typeof t==`boolean`?this.gl.uniform1i(r,+!!t):console.warn(`Unsupported uniform type for ${e}: ${typeof t}`)})};getCurrentFrame=()=>this.currentFrame;setFrame=e=>{this.currentFrame=e,this.lastRenderTime=performance.now(),this.render(performance.now())};setSpeed=(e=1)=>{this.speed=e,this.setCurrentSpeed(this.ownerDocument.hidden?0:e)};setCurrentSpeed=e=>{this.currentSpeed=e,this.rafId===null&&e!==0&&(this.lastRenderTime=performance.now(),this.rafId=requestAnimationFrame(this.render)),this.rafId!==null&&e===0&&(cancelAnimationFrame(this.rafId),this.rafId=null)};setMaxPixelCount=(e=c)=>{this.maxPixelCount=e,this.handleResize()};setMinPixelRatio=(e=2)=>{this.minPixelRatio=e,this.handleResize()};setUniforms=e=>{this.setUniformValues(e),this.providedUniforms={...this.providedUniforms,...e},this.render(performance.now())};handleDocumentVisibilityChange=()=>{this.setCurrentSpeed(this.ownerDocument.hidden?0:this.speed)};dispose=()=>{this.hasBeenDisposed=!0,this.rafId!==null&&(cancelAnimationFrame(this.rafId),this.rafId=null),this.gl&&this.program&&(this.textures.forEach(e=>{this.gl.deleteTexture(e)}),this.textures.clear(),this.gl.deleteProgram(this.program),this.program=null,this.gl.bindBuffer(this.gl.ARRAY_BUFFER,null),this.gl.bindBuffer(this.gl.ELEMENT_ARRAY_BUFFER,null),this.gl.bindRenderbuffer(this.gl.RENDERBUFFER,null),this.gl.bindFramebuffer(this.gl.FRAMEBUFFER,null),this.gl.getError()),this.resizeObserver&&=(this.resizeObserver.disconnect(),null),visualViewport?.removeEventListener(`resize`,this.handleVisualViewportChange),this.ownerDocument.removeEventListener(`visibilitychange`,this.handleDocumentVisibilityChange),this.uniformLocations={},this.canvasElement.remove(),delete this.parentElement.paperShaderMount}};function u(e,t,n){let r=e.createShader(t);return r?(e.shaderSource(r,n),e.compileShader(r),e.getShaderParameter(r,e.COMPILE_STATUS)?r:(console.error(`An error occurred compiling the shaders: `+e.getShaderInfoLog(r)),e.deleteShader(r),null)):null}function d(e,t,n){let r=e.getShaderPrecisionFormat(e.FRAGMENT_SHADER,e.MEDIUM_FLOAT),i=r?r.precision:null;i&&i<23&&(t=t.replace(/precision\s+(lowp|mediump)\s+float;/g,`precision highp float;`),n=n.replace(/precision\s+(lowp|mediump)\s+float/g,`precision highp float`).replace(/\b(uniform|varying|attribute)\s+(lowp|mediump)\s+(\w+)/g,`$1 highp $3`));let a=u(e,e.VERTEX_SHADER,t),o=u(e,e.FRAGMENT_SHADER,n);if(!a||!o)return null;let s=e.createProgram();return s?(e.attachShader(s,a),e.attachShader(s,o),e.linkProgram(s),e.getProgramParameter(s,e.LINK_STATUS)?(e.detachShader(s,a),e.detachShader(s,o),e.deleteShader(a),e.deleteShader(o),s):(console.error(`Unable to initialize the shader program: `+e.getProgramInfoLog(s)),e.deleteProgram(s),e.deleteShader(a),e.deleteShader(o),null)):null}var f=`@layer paper-shaders {
  :where([data-paper-shader]) {
    isolation: isolate;
    position: relative;

    & canvas {
      contain: strict;
      display: block;
      position: absolute;
      inset: 0;
      z-index: -1;
      width: 100%;
      height: 100%;
      border-radius: inherit;
      corner-shape: inherit;
    }
  }
}`;function p(){let e=navigator.userAgent.toLowerCase();return e.includes(`safari`)&&!e.includes(`chrome`)&&!e.includes(`android`)}function m(e){let t=visualViewport?.scale??1,n=visualViewport?.width??window.innerWidth,r=window.innerWidth-e.documentElement.clientWidth,i=t*n+r,a=outerWidth/i,o=Math.round(100*a);return o%5==0?o/100:o===33?1/3:o===67?2/3:o===133?4/3:a}var h={fit:`contain`,scale:1,rotation:0,offsetX:0,offsetY:0,originX:.5,originY:.5,worldWidth:0,worldHeight:0},g={none:0,contain:1,cover:2},_=`
#define TWO_PI 6.28318530718
#define PI 3.14159265358979323846
`,v=`
vec2 rotate(vec2 uv, float th) {
  return mat2(cos(th), sin(th), -sin(th), cos(th)) * uv;
}
`,y=`
  float hash21(vec2 p) {
    p = fract(p * vec2(0.3183099, 0.3678794)) + 0.1;
    p += dot(p, p + 19.19);
    return fract(p.x * p.y);
  }
`,b={maxColorCount:10},x=`#version 300 es
precision mediump float;

uniform float u_time;

uniform vec4 u_colors[${b.maxColorCount}];
uniform float u_colorsCount;

uniform float u_distortion;
uniform float u_swirl;
uniform float u_grainMixer;
uniform float u_grainOverlay;

in vec2 v_objectUV;
out vec4 fragColor;

${_}
${v}
${y}

float valueNoise(vec2 st) {
  vec2 i = floor(st);
  vec2 f = fract(st);
  float a = hash21(i);
  float b = hash21(i + vec2(1.0, 0.0));
  float c = hash21(i + vec2(0.0, 1.0));
  float d = hash21(i + vec2(1.0, 1.0));
  vec2 u = f * f * (3.0 - 2.0 * f);
  float x1 = mix(a, b, u.x);
  float x2 = mix(c, d, u.x);
  return mix(x1, x2, u.y);
}

float noise(vec2 n, vec2 seedOffset) {
  return valueNoise(n + seedOffset);
}

vec2 getPosition(int i, float t) {
  float a = float(i) * .37;
  float b = .6 + fract(float(i) / 3.) * .9;
  float c = .8 + fract(float(i + 1) / 4.);

  float x = sin(t * b + a);
  float y = cos(t * c + a * 1.5);

  return .5 + .5 * vec2(x, y);
}

void main() {
  vec2 uv = v_objectUV;
  uv += .5;
  vec2 grainUV = uv * 1000.;

  float grain = noise(grainUV, vec2(0.));
  float mixerGrain = .4 * u_grainMixer * (grain - .5);

  const float firstFrameOffset = 41.5;
  float t = .5 * (u_time + firstFrameOffset);

  float radius = smoothstep(0., 1., length(uv - .5));
  float center = 1. - radius;
  for (float i = 1.; i <= 2.; i++) {
    uv.x += u_distortion * center / i * sin(t + i * .4 * smoothstep(.0, 1., uv.y)) * cos(.2 * t + i * 2.4 * smoothstep(.0, 1., uv.y));
    uv.y += u_distortion * center / i * cos(t + i * 2. * smoothstep(.0, 1., uv.x));
  }

  vec2 uvRotated = uv;
  uvRotated -= vec2(.5);
  float angle = 3. * u_swirl * radius;
  uvRotated = rotate(uvRotated, -angle);
  uvRotated += vec2(.5);

  vec3 color = vec3(0.);
  float opacity = 0.;
  float totalWeight = 0.;

  for (int i = 0; i < ${b.maxColorCount}; i++) {
    if (i >= int(u_colorsCount)) break;

    vec2 pos = getPosition(i, t) + mixerGrain;
    vec3 colorFraction = u_colors[i].rgb * u_colors[i].a;
    float opacityFraction = u_colors[i].a;

    float dist = length(uvRotated - pos);

    dist = pow(dist, 3.5);
    float weight = 1. / (dist + 1e-3);
    color += colorFraction * weight;
    opacity += opacityFraction * weight;
    totalWeight += weight;
  }

  color /= max(1e-4, totalWeight);
  opacity /= max(1e-4, totalWeight);

  float grainOverlay = valueNoise(rotate(grainUV, 1.) + vec2(3.));
  grainOverlay = mix(grainOverlay, valueNoise(rotate(grainUV, 2.) + vec2(-1.)), .5);
  grainOverlay = pow(grainOverlay, 1.3);

  float grainOverlayV = grainOverlay * 2. - 1.;
  vec3 grainOverlayColor = vec3(step(0., grainOverlayV));
  float grainOverlayStrength = u_grainOverlay * abs(grainOverlayV);
  grainOverlayStrength = pow(grainOverlayStrength, .8);
  color = mix(color, grainOverlayColor, .35 * grainOverlayStrength);

  opacity += .5 * grainOverlayStrength;
  opacity = clamp(opacity, 0., 1.);

  fragColor = vec4(color, opacity);
}
`;function S(e){if(Array.isArray(e))return e.length===4?e:e.length===3?[...e,1]:O;if(typeof e!=`string`)return O;let t,n,r,i=1;if(e.startsWith(`#`))[t,n,r,i]=C(e);else if(e.startsWith(`rgb`))[t,n,r,i]=w(e);else if(e.startsWith(`hsl`))[t,n,r,i]=E(T(e));else return console.error(`Unsupported color format`,e),O;return[D(t,0,1),D(n,0,1),D(r,0,1),D(i,0,1)]}function C(e){return e=e.replace(/^#/,``),e.length===3&&(e=e.split(``).map(e=>e+e).join(``)),e.length===6&&(e+=`ff`),[parseInt(e.slice(0,2),16)/255,parseInt(e.slice(2,4),16)/255,parseInt(e.slice(4,6),16)/255,parseInt(e.slice(6,8),16)/255]}function w(e){let t=e.match(/^rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([0-9.]+))?\s*\)$/i);return t?[parseInt(t[1]??`0`)/255,parseInt(t[2]??`0`)/255,parseInt(t[3]??`0`)/255,t[4]===void 0?1:parseFloat(t[4])]:[0,0,0,1]}function T(e){let t=e.match(/^hsla?\s*\(\s*(\d+)\s*,\s*(\d+)%\s*,\s*(\d+)%\s*(?:,\s*([0-9.]+))?\s*\)$/i);return t?[parseInt(t[1]??`0`),parseInt(t[2]??`0`),parseInt(t[3]??`0`),t[4]===void 0?1:parseFloat(t[4])]:[0,0,0,1]}function E(e){let[t,n,r,i]=e,a=t/360,o=n/100,s=r/100,c,l,u;if(n===0)c=l=u=s;else{let e=(e,t,n)=>(n<0&&(n+=1),n>1&&--n,n<1/6?e+(t-e)*6*n:n<1/2?t:n<2/3?e+(t-e)*(2/3-n)*6:e),t=s<.5?s*(1+o):s+o-s*o,n=2*s-t;c=e(n,t,a+1/3),l=e(n,t,a),u=e(n,t,a-1/3)}return[c,l,u,i]}var D=(e,t,n)=>Math.min(Math.max(e,t),n),O=[0,0,0,1],k=e(t(),1);function A(e){let t=k.useRef(void 0),n=k.useCallback(t=>{let n=e.map(e=>{if(e!=null){if(typeof e==`function`){let n=e,r=n(t);return typeof r==`function`?r:()=>{n(null)}}return e.current=t,()=>{e.current=null}}});return()=>{n.forEach(e=>e?.())}},e);return k.useMemo(()=>e.every(e=>e==null)?null:e=>{t.current&&=(t.current(),void 0),e!=null&&(t.current=n(e))},e)}function j(e){if(e.naturalWidth<1024&&e.naturalHeight<1024){if(e.naturalWidth<1||e.naturalHeight<1)return;let t=e.naturalWidth/e.naturalHeight;e.width=Math.round(t>1?1024*t:1024),e.height=Math.round(t>1?1024:1024/t)}}var M=e(n(),1);async function N(e){let t={},n=[],r=e=>{try{return e.startsWith(`/`)||new URL(e),!0}catch{return!1}},i=e=>{try{return e.startsWith(`/`)?!1:new URL(e,window.location.origin).origin!==window.location.origin}catch{return!1}};return Object.entries(e).forEach(([e,a])=>{if(typeof a==`string`){let o=a||`data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==`;if(!r(o)){console.warn(`Uniform "${e}" has invalid URL "${o}". Skipping image loading.`);return}let s=new Promise((n,r)=>{let a=new Image;i(o)&&(a.crossOrigin=`anonymous`),a.onload=()=>{j(a),t[e]=a,n()},a.onerror=()=>{console.error(`Could not set uniforms. Failed to load image at ${o}`),r()},a.src=o});n.push(s)}else a instanceof HTMLImageElement&&j(a),t[e]=a}),await Promise.all(n),t}var P=(0,k.forwardRef)(function({fragmentShader:e,uniforms:t,webGlContextAttributes:n,speed:r=0,frame:i=0,width:a,height:o,minPixelRatio:s,maxPixelCount:c,mipmaps:u,style:d,...f},p){let[m,h]=(0,k.useState)(!1),g=(0,k.useRef)(null),_=(0,k.useRef)(null),v=(0,k.useRef)(n);return(0,k.useEffect)(()=>((async()=>{let n=await N(t);g.current&&!_.current&&(_.current=new l(g.current,e,n,v.current,r,i,s,c,u),h(!0))})(),()=>{_.current?.dispose(),_.current=null}),[e]),(0,k.useEffect)(()=>{let e=!1;return(async()=>{let n=await N(t);e||_.current?.setUniforms(n)})(),()=>{e=!0}},[t,m]),(0,k.useEffect)(()=>{_.current?.setSpeed(r)},[r,m]),(0,k.useEffect)(()=>{_.current?.setMaxPixelCount(c)},[c,m]),(0,k.useEffect)(()=>{_.current?.setMinPixelRatio(s)},[s,m]),(0,k.useEffect)(()=>{_.current?.setFrame(i)},[i,m]),(0,M.jsx)(`div`,{ref:A([g,p]),style:a!==void 0||o!==void 0?{width:typeof a==`string`&&isNaN(+a)===!1?+a:a,height:typeof o==`string`&&isNaN(+o)===!1?+o:o,...d}:d,...f})});P.displayName=`ShaderMount`;function F(e,t){for(let n in e){if(n===`colors`){let n=Array.isArray(e.colors),r=Array.isArray(t.colors);if(!n||!r){if(Object.is(e.colors,t.colors)===!1)return!1;continue}if(e.colors?.length!==t.colors?.length||!e.colors?.every((e,n)=>e===t.colors?.[n]))return!1;continue}if(Object.is(e[n],t[n])===!1)return!1}return!0}var I={name:`Default`,params:{...h,speed:1,frame:0,colors:[`#e0eaff`,`#241d9a`,`#f75092`,`#9f50d3`],distortion:.8,swirl:.1,grainMixer:0,grainOverlay:0}};({...h}),{...h},{...h};var L=(0,k.memo)(function({speed:e=I.params.speed,frame:t=I.params.frame,colors:n=I.params.colors,distortion:r=I.params.distortion,swirl:i=I.params.swirl,grainMixer:a=I.params.grainMixer,grainOverlay:o=I.params.grainOverlay,fit:s=I.params.fit,rotation:c=I.params.rotation,scale:l=I.params.scale,originX:u=I.params.originX,originY:d=I.params.originY,offsetX:f=I.params.offsetX,offsetY:p=I.params.offsetY,worldWidth:m=I.params.worldWidth,worldHeight:h=I.params.worldHeight,..._}){let v={u_colors:n.map(S),u_colorsCount:n.length,u_distortion:r,u_swirl:i,u_grainMixer:a,u_grainOverlay:o,u_fit:g[s],u_rotation:c,u_scale:l,u_offsetX:f,u_offsetY:p,u_originX:u,u_originY:d,u_worldWidth:m,u_worldHeight:h};return(0,M.jsx)(P,{..._,speed:e,frame:t,fragmentShader:x,uniforms:v})},F),R=`/music/static/assets/4809f7fdde3b0876d88326d566fecd79-BE-nQlTK.png`,z=`/music/static/assets/8ed68a79fef4927d3b2065c425250ef9-BekuEHg6.png`,B=`/music/static/assets/81a999a639b9f77ee80d000a0f441cdb-B0ncnPGL.png`,V=({enabled:e=!0,framesPerSecond:t,shaderRef:n,speed:r})=>{(0,k.useEffect)(()=>{let i=n.current;if(!e||!i||t<=0||r===0)return;let a=window.matchMedia(`(prefers-reduced-motion: reduce)`),o=1e3/t,s=null,c=i.paperShaderMount?.getCurrentFrame()??0,l=!1,u=null,d=null,f=e=>{d!==null&&(c+=(e-d)*r),d=e,(u===null||e-u>=o)&&(i.paperShaderMount?.setFrame(c),u=e),s=window.requestAnimationFrame(f)},p=()=>{s!==null&&(window.cancelAnimationFrame(s),s=null),u=null,d=null},m=()=>{if(!(l&&!document.hidden&&!a.matches)){p();return}s===null&&(s=window.requestAnimationFrame(f))},h=new IntersectionObserver(([e])=>{l=e?.isIntersecting??!1,m()}),g=()=>{m()},_=()=>{m()};return h.observe(i),document.addEventListener(`visibilitychange`,g),a.addEventListener(`change`,_),()=>{p(),h.disconnect(),document.removeEventListener(`visibilitychange`,g),a.removeEventListener(`change`,_)}},[e,t,n,r])},H=`var(--semi-color-white-50, rgba(255, 255, 255, 0.5))`,U=30,W=96e3,G={likes:R,recent:z,recentlyAdded:B,heartbeat:`/music/static/assets/heartbeat_cover.png`,downloads:`/music/static/assets/downloads_cover.png`,daily:`/music/static/assets/daily_cover.png`},K={likes:{colors:[`#FF6B35`,`#FF1744`,`#FF9100`,`#FFD740`],speed:1.8,scale:.9,distortion:.6,swirl:.2},recent:{colors:[`#0A2A1A`,`#1B5E3B`,`#77B5A0`,`#2ECC71`],speed:1.3,scale:1.5,distortion:.5,swirl:.08},recentlyAdded:{colors:[`#3C3C3C`,`#777777`,`#B7B7B7`,`#545454`],speed:1.1,scale:1.4,distortion:.5,swirl:.06},heartbeat:{colors:[`#F43F5E`,`#E11D48`,`#A855F7`,`#7C3AED`],speed:1.6,scale:1.2,distortion:.6,swirl:.15},downloads:{colors:[`#06B6D4`,`#0EA5E9`,`#0D9488`,`#10B981`],speed:1.4,scale:1.2,distortion:.5,swirl:.1}};function q(){return(0,M.jsx)(`svg`,{width:`72`,height:`72`,viewBox:`0 0 72 72`,fill:`none`,"aria-hidden":`true`,style:{position:`relative`,zIndex:1},children:(0,M.jsx)(`path`,{d:`M22.5 12.0015C13.3873 12.0015 6 19.3888 6 28.5015C6 45.0015 25.5 60.0015 36 63.4908C46.5 60.0015 66 45.0015 66 28.5015C66 19.3888 58.6126 12.0015 49.5 12.0015C43.9196 12.0015 38.9861 14.7718 36 19.0122C33.0139 14.7718 28.0804 12.0015 22.5 12.0015Z`,fill:H})})}function J(){return(0,M.jsx)(`svg`,{width:`72`,height:`72`,viewBox:`0 0 72 72`,fill:`none`,"aria-hidden":`true`,style:{position:`relative`,zIndex:1},children:(0,M.jsx)(`path`,{d:`M35.9985 5.99854C52.5671 5.99854 65.9985 19.43 65.9985 35.9985C65.9985 52.5671 52.5671 65.9985 35.9985 65.9985C19.43 65.9985 5.99854 52.5671 5.99854 35.9985C5.99854 19.43 19.43 5.99854 35.9985 5.99854ZM36.0024 19.8003C34.5113 19.8001 33.3024 21.0093 33.3022 22.5005L33.3003 35.688V36.7798L34.0591 37.5649L43.0591 46.8765C44.0954 47.9481 45.8044 47.9778 46.8765 46.9419C47.9485 45.9058 47.9775 44.1968 46.9419 43.1245L38.7007 34.5972L38.7017 22.5005C38.7017 21.0097 37.4931 19.8009 36.0024 19.8003Z`,fill:H})})}function HeartbeatIcon(){return(0,M.jsxs)(`svg`,{width:`80`,height:`80`,viewBox:`0 0 24 24`,fill:`none`,stroke:`currentColor`,strokeWidth:`1.8`,strokeLinecap:`round`,strokeLinejoin:`round`,style:{position:`relative`,zIndex:1,filter:`drop-shadow(0 4px 16px rgba(246,44,85,0.7))`},children:[(0,M.jsx)(`path`,{d:`M19.5 12.572l-7.5 7.428l-7.5 -7.428a5 5 0 1 1 7.5 -6.566a5 5 0 1 1 7.5 6.572`,stroke:`rgba(255,255,255,0.9)`}),(0,M.jsx)(`path`,{d:`M7 13l2.5 0l1.5 -3l2 6l1.5 -3l2.5 0`,stroke:`#ffffff`,strokeWidth:`2`})]})}
function DownloadsIcon(){return(0,M.jsxs)(`svg`,{width:`76`,height:`76`,viewBox:`0 0 24 24`,fill:`none`,stroke:`currentColor`,strokeWidth:`2`,strokeLinecap:`round`,strokeLinejoin:`round`,style:{position:`relative`,zIndex:1,filter:`drop-shadow(0 4px 16px rgba(20,184,166,0.7))`},children:[(0,M.jsx)(`path`,{d:`M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2 -2v-2`,stroke:`rgba(255,255,255,0.85)`}),(0,M.jsx)(`polyline`,{points:`7 11 12 16 17 11`,stroke:`#ffffff`,strokeWidth:`2.2`}),(0,M.jsx)(`line`,{x1:`12`,y1:`4`,x2:`12`,y2:`16`,stroke:`#ffffff`,strokeWidth:`2.2`})]})}
function DailyClockIcon(){return(0,M.jsxs)(`svg`,{width:`80`,height:`80`,viewBox:`0 0 24 24`,fill:`none`,stroke:`currentColor`,strokeWidth:`1.8`,strokeLinecap:`round`,strokeLinejoin:`round`,style:{position:`relative`,zIndex:1,filter:`drop-shadow(0 4px 16px rgba(245,158,11,0.7))`},children:[(0,M.jsx)(`circle`,{cx:`12`,cy:`12`,r:`9`,stroke:`rgba(255,255,255,0.95)`,strokeWidth:`1.8`}),(0,M.jsx)(`polyline`,{points:`12 7 12 12 15 15`,stroke:`#ffffff`,strokeWidth:`2.2`}),(0,M.jsx)(`path`,{d:`M12 3v-1M12 22v-1M3 12h-1M22 12h-1`,stroke:`rgba(255,255,255,0.7)`,strokeWidth:`2`})]})}
function Y(){return(0,M.jsx)(i,{"aria-hidden":`true`,className:`relative z-[1] text-[72px]`,color:H})}var X=({borderRadius:e=24,className:t,glyphClassName:n,variant:i})=>{
  let s=K[i]||K.likes,c=a(),l=o(e=>e.isNowPlayingOpen),u=(0,k.useRef)(null),d={borderRadius:e,clipPath:`inset(0 round ${e}px)`};
  return V({enabled:c&&!l,framesPerSecond:U,shaderRef:u,speed:s.speed}),(0,M.jsx)(`div`,{className:r(`relative h-[200px] w-[200px] shrink-0`,t),children:(0,M.jsxs)(`div`,{className:`flex h-full w-full items-center justify-center overflow-hidden`,style:{...d,position:`relative`},children:[
    c?(0,M.jsx)(L,{ref:u,className:`overflow-hidden [&>canvas]:[border-radius:inherit] [&>canvas]:[clip-path:inherit]`,colors:s.colors,maxPixelCount:W,minPixelRatio:1,speed:0,scale:s.scale,distortion:s.distortion,swirl:s.swirl,style:{...d,position:`absolute`,inset:0,width:`100%`,height:`100%`}}):(0,M.jsx)(`img`,{alt:``,"aria-hidden":`true`,className:`absolute inset-0 h-full w-full object-cover`,draggable:!1,src:(G[i]||G.likes)}),
    (0,M.jsx)(`div`,{className:r(`relative z-[1] flex items-center justify-center`,n),children:
      (i===`likes`?(0,M.jsx)(q,{}):
       i===`recent`?(0,M.jsx)(J,{}):
       i===`recentlyAdded`?(0,M.jsx)(Y,{}):
       i===`heartbeat`?(0,M.jsx)(HeartbeatIcon,{}):
       i===`downloads`?(0,M.jsx)(DownloadsIcon,{}):
       i===`daily`?(0,M.jsx)(DailyClockIcon,{}):
       (0,M.jsx)(q,{}))
    })
  ]})})
};export{X as t};