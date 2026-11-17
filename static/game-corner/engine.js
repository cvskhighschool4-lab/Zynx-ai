import * as THREE from "three";
import * as CANNON from "cannon-es";
export {THREE,CANNON};
export class Engine{
 constructor(host){
  this.host=host;this.scene=new THREE.Scene();this.scene.background=new THREE.Color(0x070b13);this.scene.fog=new THREE.FogExp2(0x070b13,.012);
  this.camera=new THREE.PerspectiveCamera(65,1,.1,420);this.camera.position.set(0,5,11);
  this.renderer=new THREE.WebGLRenderer({antialias:true,alpha:false,powerPreference:"high-performance"});
  this.renderer.setPixelRatio(Math.min(devicePixelRatio||1,1.5));this.renderer.setSize(host.clientWidth,host.clientHeight);
  this.renderer.outputColorSpace=THREE.SRGBColorSpace;this.renderer.toneMapping=THREE.ACESFilmicToneMapping;this.renderer.toneMappingExposure=1.15;
  this.renderer.shadowMap.enabled=true;this.renderer.shadowMap.type=THREE.PCFSoftShadowMap;host.appendChild(this.renderer.domElement);
  this.world=new CANNON.World({gravity:new CANNON.Vec3(0,-9.82,0)});this.world.broadphase=new CANNON.SAPBroadphase(this.world);this.world.allowSleep=true;this.world.solver.iterations=8;
  this.ambient=new THREE.HemisphereLight(0x9bbcff,0x10121c,1.7);this.scene.add(this.ambient);
  this.key=new THREE.DirectionalLight(0xdceaff,2.5);this.key.position.set(-8,15,9);this.key.castShadow=true;this.key.shadow.mapSize.set(1024,1024);this.key.shadow.camera.left=-28;this.key.shadow.camera.right=28;this.key.shadow.camera.top=28;this.key.shadow.camera.bottom=-28;this.scene.add(this.key);
  this.fill=new THREE.PointLight(0x18dfff,22,48);this.fill.position.set(5,5,-8);this.scene.add(this.fill);
  this.fixed=1/60;this.acc=0;this.last=0;this.game=null;this.paused=false;this.fx=false;this.composer=null;this.fxaaPass=null;this.frames=0;this.fps=60;this.resize=new ResizeObserver(()=>this.onResize());this.resize.observe(host);
  this.loop=this.loop.bind(this);this.renderer.setAnimationLoop(this.loop);
 }
 onResize(){const w=Math.max(1,this.host.clientWidth),h=Math.max(1,this.host.clientHeight);this.camera.aspect=w/h;this.camera.updateProjectionMatrix();this.renderer.setSize(w,h);this.renderer.setPixelRatio(Math.min(devicePixelRatio||1,1.5));this.composer?.setSize(w,h);if(this.fxaaPass){const dpr=this.renderer.getPixelRatio();this.fxaaPass.material.uniforms.resolution.value.set(1/(w*dpr),1/(h*dpr));}}
 loop(now){if(!this.last)this.last=now;const frame=Math.min((now-this.last)/1000,.05);this.last=now;if(!this.paused&&this.game){this.acc+=frame;let steps=0;while(this.acc>=this.fixed&&steps<3){this.game.beforeStep?.(this.fixed);this.world.step(this.fixed);this.game.update?.(this.fixed);this.acc-=this.fixed;steps++;}if(steps===3)this.acc=0;}
  if(this.composer)this.composer.render();else this.renderer.render(this.scene,this.camera);this.frames++;if(now-this.fpsAt>1000){this.fps=this.frames;this.frames=0;this.fpsAt=now;}
 }
 setGame(game){this.game=game;}
 setPaused(v){this.paused=v;}
 async setFX(enabled){
  this.fx=enabled;this.renderer.toneMappingExposure=enabled?1.15:1;this.renderer.shadowMap.enabled=enabled;
  if(enabled&&!this.composer){
   try{
    const [EC,RP,UB,SP,FX,OP]=await Promise.all([
     import("three/addons/postprocessing/EffectComposer.js"),
     import("three/addons/postprocessing/RenderPass.js"),
     import("three/addons/postprocessing/UnrealBloomPass.js"),
     import("three/addons/postprocessing/ShaderPass.js"),
     import("three/addons/shaders/FXAAShader.js"),
     import("three/addons/postprocessing/OutputPass.js")
    ]);
    this.composer=new EC.EffectComposer(this.renderer);
    this.composer.addPass(new RP.RenderPass(this.scene,this.camera));
    this.composer.addPass(new UB.UnrealBloomPass(new THREE.Vector2(this.host.clientWidth,this.host.clientHeight),.48,.35,.82));
    this.fxaaPass=new SP.ShaderPass(FX.FXAAShader);
    const dpr=this.renderer.getPixelRatio();this.fxaaPass.material.uniforms.resolution.value.set(1/(this.host.clientWidth*dpr),1/(this.host.clientHeight*dpr));
    this.composer.addPass(this.fxaaPass);this.composer.addPass(new OP.OutputPass());
   }catch(err){console.warn("Post FX unavailable; using standard renderer",err);this.composer=null;this.fx=false}
  }
  if(!enabled&&this.composer){this.composer.dispose?.();this.composer=null;this.fxaaPass=null;}
  this.renderer.setPixelRatio(Math.min(devicePixelRatio||1,enabled?1.5:1));
 }
 dispose(){this.renderer.setAnimationLoop(null);this.resize.disconnect();this.game?.dispose?.();this.world.bodies.slice().forEach(b=>this.world.removeBody(b));this.scene.traverse(o=>{o.geometry?.dispose?.();if(o.material){(Array.isArray(o.material)?o.material:[o.material]).forEach(m=>m.dispose?.())}});this.composer?.dispose?.();this.renderer.dispose();this.renderer.domElement.remove();}
}
export function mat(color,emissive=0x000000,roughness=.5){return new THREE.MeshStandardMaterial({color,emissive,emissiveIntensity:emissive?1.4:0,metalness:.35,roughness});}
export function box(scene,pos,size,material){const m=new THREE.Mesh(new THREE.BoxGeometry(...size),material);m.position.set(...pos);m.castShadow=true;m.receiveShadow=true;scene.add(m);return m;}
export function plane(scene,pos,size,material){const m=new THREE.Mesh(new THREE.PlaneGeometry(...size),material);m.rotation.x=-Math.PI/2;m.position.set(...pos);m.receiveShadow=true;scene.add(m);return m;}
export function bodyBox(world,pos,size,mass=0){const b=new CANNON.Body({mass,position:new CANNON.Vec3(...pos),shape:new CANNON.Box(new CANNON.Vec3(size[0]/2,size[1]/2,size[2]/2)),linearDamping:.22,angularDamping:.85});world.addBody(b);return b;}
export function follow(engine,target,offset,lerp=.09){const desired=new THREE.Vector3(target.x+offset[0],target.y+offset[1],target.z+offset[2]);engine.camera.position.lerp(desired,lerp);engine.camera.lookAt(target.x,target.y,target.z);}
