import {defineConfig} from "vite";
import {fileURLToPath} from "node:url";
import {resolve} from "node:path";
const here=fileURLToPath(new URL(".",import.meta.url));
export default defineConfig({
 root:resolve(here,"../static/game-corner"),
 base:"/game-corner/",
 build:{outDir:resolve(here,"../static/game-corner-dist"),emptyOutDir:true,rollupOptions:{output:{manualChunks:{three:["three"],physics:["cannon-es"]}}}}
});