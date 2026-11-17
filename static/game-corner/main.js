import {Engine} from "./engine.js";
import {GAME_FACTORIES} from "./games.js";
import {audio} from "./audio.js";
const $=s=>document.querySelector(s),selector=$("#selector"),view=$("#gameView"),stage=$("#stage"),loading=$("#loading"),keys=new Set();
let engine=null,active=null,current=null,paused=false,scoreValue=0,toastTimer=0;
const meta={neon:["01","Neon Velocity","REACH CHECKPOINTS","WASD / ARROWS · SPACE = NITRO"],aether:["02","Aether Drift","FLY THROUGH GATES","WASD / ARROWS · SPACE = BOOST"],iron:["03","Iron Legion","COLLECT REACTOR CELLS","WASD / ARROWS · SPACE = JUMP"],void:["04","Void Runner","KEEP YOUR RUN ALIVE","← → TO SWITCH LANES · SPACE = JUMP"],pulse:["05","Pulse Striker","LIGHT UP TARGET RINGS","WASD / ARROWS · SPACE = PULSE"]};
const ui={keys,objective:t=>$("#objective").textContent=t,score:n=>{scoreValue=n;$("#score").textContent="SCORE "+String(n).padStart(6,"0")},toast:t=>{const el=$("#toast");el.textContent=t;el.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove("show"),1300)},sound:n=>audio[n]?.()};
function dispose(){if(engine){engine.dispose();engine=null}active=null;stage.querySelectorAll("canvas").forEach(c=>c.remove())}
async function launch(id){current=id;selector.classList.add("hidden");view.classList.remove("hidden");loading.classList.remove("hidden");const [num,name,objective,hint]=meta[id];$("#gameNumber").textContent=num;$("#gameName").textContent=name;$("#hudWorld").textContent=name.toUpperCase();$("#objective").textContent=objective;$("#controlHint").textContent=hint;ui.score(0);keys.clear();audio.start();dispose();try{engine=new Engine(stage);active=GAME_FACTORIES[id](engine,ui);engine.setGame(active);loading.classList.add("hidden");paused=false;$("#pauseOverlay").classList.add("hidden");$("#pauseGame").textContent="PAUSE";$("#best").textContent=localStorage.getItem("zynx-best-"+id)||"000000"}catch(err){console.error(err);loading.textContent="WORLD FAILED TO INITIALIZE — TRY RESTART";loading.classList.remove("hidden")}}
function exit(){dispose();view.classList.add("hidden");selector.classList.remove("hidden");keys.clear()}
function togglePause(){if(!engine)return;paused=!paused;engine.setPaused(paused);$("#pauseOverlay").classList.toggle("hidden",!paused);$("#pauseGame").textContent=paused?"RESUME":"PAUSE"}
document.querySelectorAll("[data-game]").forEach(b=>b.addEventListener("click",()=>launch(b.dataset.game)));
$("#exitGame").addEventListener("click",exit);$("#pauseGame").addEventListener("click",togglePause);$("#resumeGame").addEventListener("click",togglePause);$("#restartGame").addEventListener("click",()=>current&&launch(current));
$("#fxToggle").addEventListener("click",async()=>{if(!engine)return;const on=!engine.fx;await engine.setFX(on);$("#fxToggle").textContent=on?"FX ON":"FX LOW"});
window.addEventListener("keydown",e=>{if(["ArrowUp","ArrowDown","ArrowLeft","ArrowRight","Space"].includes(e.code))e.preventDefault();if(e.code==="Escape"&&engine){togglePause();return}keys.add(e.code)});
window.addEventListener("keyup",e=>keys.delete(e.code));window.addEventListener("blur",()=>{keys.clear();if(engine&&!paused)togglePause()});
document.querySelectorAll("[data-key]").forEach(b=>{const k=b.dataset.key;const down=e=>{e.preventDefault();keys.add(k)},up=e=>{e.preventDefault();keys.delete(k)};b.addEventListener("pointerdown",down);b.addEventListener("pointerup",up);b.addEventListener("pointercancel",up);b.addEventListener("pointerleave",up)});
if("ontouchstart"in window)$("#mobileControls").style.display="flex";
