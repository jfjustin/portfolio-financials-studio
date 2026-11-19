import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
import {resolve,dirname} from 'node:path';
const directory=dirname(fileURLToPath(import.meta.url));
await build({absWorkingDir:directory,entryPoints:['src/main.tsx'],bundle:true,minify:true,outdir:resolve(directory,'../static/portfolio-dashboard'),entryNames:'dashboard',jsx:'automatic',platform:'browser',target:['es2022'],define:{'process.env.NODE_ENV':'"production"'},legalComments:'eof'});
