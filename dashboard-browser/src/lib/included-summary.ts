import type {Summary,Row} from './portfolio-summary';
// A deployment can supply a private default snapshot. No financial figures are
// embedded in this repository. Uploaded workbooks never use this server path.
function numeric(value:unknown):number|null{
  if(value===null||value===undefined||value===''||value==='—')return null;
  if(typeof value==='number')return Number.isFinite(value)?value:null;
  const text=String(value).replace(/[\s$,Mx]/g,'').replace('−','-');
  const n=Number(text.replace('%',''));return Number.isFinite(n)?(text.includes('%')?n/100:n):null;
}
export function initialSummary(input:unknown):Summary|null{
  if(!input||typeof input!=='object')return null;
  const data=input as Record<string,unknown>;
  // Accept the private snapshot format used by the original dashboard PR.
  if(Array.isArray(data.sections)&&data.sections.length===2){
    if(!data.sections.every(section=>section&&typeof section==='object'&&Array.isArray((section as {rows:unknown}).rows)&&(section as {rows:unknown[]}).rows.every(Array.isArray)))return null;
    const convert=(section:{rows:unknown[][]},sourceRows:number[]):Row[]=>section.rows.map((r,i)=>({name:String(r[0]),sourceRow:sourceRows[i],values:r.slice(1).map(numeric)}));
    const sections=data.sections as {rows:unknown[][]}[];
    input={direct:convert(sections[0],[80,81,82,83,85]),portfolio:convert(sections[1],[88,89,90]),irr:Array.isArray(data.irr)?data.irr.map(numeric):[],date:typeof data.period==='string'?data.period:null,fundDate:null,filename:'Configured portfolio snapshot'};
  }
  const s=input as Summary;
  const validRow=(r:Row,columns:number)=>r&&typeof r.name==='string'&&Array.isArray(r.values)&&r.values.length===columns&&r.values.every((v,i)=>i===columns-1&&v===null||typeof v==='number'&&Number.isFinite(v));
  if(!Array.isArray(s.direct)||s.direct.length!==5||!s.direct.every(r=>validRow(r,12)))return null;
  if(!Array.isArray(s.portfolio)||s.portfolio.length!==3||!s.portfolio.every(r=>validRow(r,14)))return null;
  if(!Array.isArray(s.irr)||s.irr.length!==2||!s.irr.every(v=>typeof v==='number'&&Number.isFinite(v)))return null;
  return {...s,date:typeof s.date==='string'?s.date:null,fundDate:typeof s.fundDate==='string'?s.fundDate:null,filename:typeof s.filename==='string'?s.filename:'Configured portfolio snapshot'};
}
function readSnapshot(){
  const element=document.getElementById('portfolio-dashboard-snapshot');
  if(!element?.textContent)return null;
  try{return initialSummary(JSON.parse(element.textContent));}catch{return null;}
}
export const INCLUDED_SUMMARY=typeof document==='undefined'?null:readSnapshot();
