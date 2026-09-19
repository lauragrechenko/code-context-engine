// Prototype for SPEC-elixir-markdown-chunking.md §2.1 — the Elixir walk (R1-R8),
// run over a whole repo to measure chunk yield and byte coverage before any
// Python is written. Uses codegraph's vendored tree-sitter-elixir wasm so no
// package has to be installed to reproduce the numbers. Both paths below are
// absolute on purpose: the wasm grammar lives in the codegraph checkout.
//
//   node spec-prototypes/elixir_yield.mjs
//
// Measured 2026-09-19 on crypto-key-enclave (363 files, 2.6 MB):
//   preamble=492 function=2270 block-macro=1735 clauses merged=731
//   total chunks after windowing = 4896 (oversize leaves=364)
//   coverage = 98.5%
const WASM = '/Users/lauragrechenko/learning/AI/codegraph/lg/codegraph/src/extraction/wasm/tree-sitter-elixir.wasm';
const ROOT = process.argv[2] ?? '/Users/lauragrechenko/work/esl/clavium/crypto-key-enclave';

import { Parser, Language } from '/Users/lauragrechenko/learning/AI/codegraph/lg/codegraph/node_modules/web-tree-sitter/tree-sitter.js';
import fs from 'fs'; import { execSync } from 'child_process';
await Parser.init();
const lang = await Language.load(WASM);
const p = new Parser(); p.setLanguage(lang);
const files = execSync('git ls-files',{cwd:ROOT}).toString().split('\n').filter(f=>/\.exs?$/.test(f));
const DEF=new Set(['def','defp','defmacro','defmacrop','defguard','defguardp','defdelegate','defn','defnp']);
const MOD=new Set(['defmodule','defprotocol','defimpl']);
const DIR=new Set(['alias','import','require','use']);
const WIN=1500, OV=200;
let stats={hdr:0,fn:0,block:0,merged:0,chunks:0,oversize:0,extra:0,cov:0,tot:0,recursed:0}; const big=[];
const ident=(n,src)=>{const c=n.namedChildren[0];return c&&c.type==='identifier'?src.slice(c.startIndex,c.endIndex):null;};

// R5: unwrap `when` guards, then identifier -> arity 0, call -> arity = argc.
function headName(call,src){
  const args=call.namedChildren.find(c=>c.type==='arguments'); if(!args) return null;
  let h=args.namedChildren.filter(c=>c.type!=='keywords')[0]; if(!h) return null;
  while(h.type==='binary_operator'){const l=h.childForFieldName('left');const op=h.childForFieldName('operator');
    if(!l||!op||src.slice(op.startIndex,op.endIndex)!=='when') break; h=l;}
  if(h.type==='identifier') return src.slice(h.startIndex,h.endIndex)+'/0';
  if(h.type==='call'){const n=ident(h,src);const a=h.namedChildren.find(c=>c.type==='arguments');
    return (n??'?')+'/'+(a?a.namedChildCount:0);}
  return null;
}
const windows=len=>Math.max(1,Math.ceil(Math.max(0,len-OV)/(WIN-OV)));

function emitChunk(start,end,kind,file,node,src){
  const len=end-start; if(len<=0) return;
  // R8: recurse into oversized containers (describe holding tests), never into
  // a function — a def's internal case/with are do_block calls too.
  if(len>WIN*2 && node && src && kind!=='fn'){
    const blk=node.namedChildren&&node.namedChildren.find(c=>c.type==='do_block');
    if(blk&&blk.namedChildren.some(c=>c.type==='call'&&c.namedChildren.some(g=>g.type==='do_block'))){
      stats.recursed++; walkBlock(blk,src,file,blk.startIndex); return;
    }
  }
  stats.cov+=len;
  const w=windows(len); stats.chunks+=w; if(w>1){stats.oversize++;stats.extra+=w-1;big.push([len,file,kind]);}
}

function doModule(call,src,file){
  const blk=call.namedChildren.find(c=>c.type==='do_block');
  if(!blk){ emitChunk(call.startIndex,call.endIndex,'mod-nobody',file); stats.hdr++; return; }
  walkBlock(blk,src,file,call.startIndex);
}

function walkBlock(blk,src,file,preAnchor){
  let preStart=preAnchor, pending=null, last=null;
  const flushPre=(end)=>{ if(end>preStart){ emitChunk(preStart,end,'preamble',file); stats.hdr++; } preStart=null; };
  for(const ch of blk.namedChildren){
    const isCall=ch.type==='call'; const id=isCall?ident(ch,src):null;
    const hasDo=isCall&&ch.namedChildren.some(c=>c.type==='do_block');
    const isDef=id&&DEF.has(id); const isMod=id&&MOD.has(id);
    const isBoundary=isDef||isMod||(hasDo&&id&&!DIR.has(id));   // R1
    if(!isBoundary){ if(pending===null) pending=ch.startIndex; continue; }  // R4
    const start=pending??ch.startIndex; pending=null;
    if(preStart!==null) flushPre(start);                        // R2
    if(isMod){ if(last){emitChunk(last.s,last.e,last.k,file,last.n,src); last=null;} doModule(ch,src,file); continue; } // R7
    const key=isDef?headName(ch,src):null;
    if(last&&key&&last.key===key){ last.e=ch.endIndex; stats.merged++; continue; } // R3 (uncapped here)
    if(last) emitChunk(last.s,last.e,last.k,file,last.n,src);
    last={key,s:start,e:ch.endIndex,k:isDef?'fn':'block',n:ch}; isDef?stats.fn++:stats.block++;
  }
  if(preStart!==null) flushPre(blk.endIndex);
  if(last) emitChunk(last.s,last.e,last.k,file,last.n,src);
  if(pending!==null) emitChunk(pending,blk.endIndex,'trailer',file);
}

for(const f of files){
  const src=fs.readFileSync(ROOT+'/'+f,'utf8'); stats.tot+=src.length;
  const tree=p.parse(src); let any=false;
  for(const n of tree.rootNode.namedChildren) if(n.type==='call'&&MOD.has(ident(n,src)??'')){any=true;doModule(n,src,f);}
  if(!any){ emitChunk(0,src.length,'whole',f); }
}
big.sort((a,b)=>b[0]-a[0]);
console.log(`preamble=${stats.hdr} function=${stats.fn} block-macro(test/describe/setup/schema)=${stats.block} clauses merged=${stats.merged}`);
console.log(`total chunks after windowing = ${stats.chunks} (oversize leaves=${stats.oversize}, extra windows=${stats.extra})`);
console.log(`coverage = ${(100*stats.cov/stats.tot).toFixed(1)}% of ${stats.tot.toLocaleString()} bytes`);
console.log(`oversize blocks recursed into = ${stats.recursed}`);
console.log('largest leaves:'); for(const [l,f,k] of big.slice(0,6)) console.log(`  ${String(l).padStart(6)}B ${k.padEnd(9)} ${f}`);
