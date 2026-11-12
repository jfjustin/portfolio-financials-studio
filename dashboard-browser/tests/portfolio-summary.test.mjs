import test from 'node:test';
import assert from 'node:assert/strict';
import {parseSummary,display,csvFor} from '../src/lib/portfolio-summary.ts';
function fixture(){
 const data=Array.from({length:94},()=>Array(18).fill(null));const set=(r,c,v)=>data[r-1][c-1]=v;
 for(const r of [79,87])for(const [c,label] of [[5,'# of Company'],[7,'Total Value'],[8,'Book Value'],[9,'Impairment'],[11,'Realized Gain/(Loss)'],[12,'Unrealized Gain/(Loss)'],[13,'Total Gain/(Loss)'],[15,'TVPI'],[18,'% Capital Allocation']])set(r,c,label);
 for(const [r,c,label] of [[79,6,'Investment cost'],[79,10,'Total Received Proceeds'],[79,14,'Gain/Loss (%)'],[87,6,'Total investment (Paid-in Capital)'],[87,10,'Proceeds & Distribution'],[87,14,'Gain/Loss'],[87,16,'DPI'],[87,17,'AUM'],[93,3,'IRR for Core Investments'],[94,3,'IRR for Life Science Investments']])set(r,c,label);
 const names=['Current Direct Investment','Impaired Investment','Exit Investment','Total Direct Investment','Fund Investment','Total Investments','Life Sciences Investments','Core Investments'];
 [80,81,82,83,85,88,89,90].forEach((r,i)=>{set(r,3,names[i]);for(let c=5;c<=(r<87?15:17);c++)set(r,c,r*100+c);});
 set(93,5,.15);set(94,5,.12);set(5,12,"Q2'27 NAV");return data;
}
test('maps exact cells, optional allocation blanks, and reporting dates',()=>{const s=parseSummary(fixture(),'new.xlsx',new Date('2027-07-10T00:00:00Z'));assert.equal(s.portfolio[0].values[1],8806);assert.equal(s.direct[4].values[7],8512);assert.equal(s.direct[3].values[11],null);assert.deepEqual(s.irr,[.15,.12]);assert.equal(s.date,'July 10, 2027');assert.equal(s.fundDate,'June 30, 2027');});
test('rejects shifted headers rather than guessing',()=>{const d=fixture();d[78][10]='Unrealized Gain/(Loss)';assert.throws(()=>parseSummary(d,'x.xlsx'),/K79/);});
test('rejects mismatched category rows',()=>{const d=fixture();d[79][2]='Fund Investment';assert.throws(()=>parseSummary(d,'x.xlsx'),/C80/);});
test('missing formula values fail instead of becoming zero',()=>{const d=fixture();d[87][6]=null;assert.throws(()=>parseSummary(d,'x.xlsx'),/G88/);});
test('valid zeros and negative values survive',()=>{const d=fixture();d[79][8]=0;d[84][11]=-2.3;const s=parseSummary(d,'x.xlsx');assert.equal(s.direct[0].values[4],0);assert.equal(s.direct[4].values[7],-2.3);assert.equal(display(-2.3,7),'-$2.3M');});
test('dates are unavailable when missing, never fixed to June 2026',()=>{const d=fixture();d[4][11]=null;const s=parseSummary(d,'x.xlsx');assert.equal(s.date,null);assert.equal(s.fundDate,null);});
test('CSV includes every source section and category',()=>{const csv=csvFor(parseSummary(fixture(),'x.xlsx'));assert.match(csv,/Core Investments/);assert.match(csv,/Fund Investment/);assert.match(csv,/IRR Life Sciences/);assert.match(csv,/15.0%/);});
if(process.env.WORKBOOK_TEST_PATH)test('supplied workbook parses its saved results',async()=>{const {readSheet}=await import('read-excel-file/node');const path=process.env.WORKBOOK_TEST_PATH;const rows=await readSheet(path,'Summary-Portfolio Value');const update=await readSheet(path,'UPDATE DATE');const s=parseSummary(rows,'provided.xlsx',update[8]?.[4]);for(const row of [...s.direct,...s.portfolio]){const cols=row.sourceRow<87?[5,6,7,8,9,10,11,12,13,14,15,18]:[5,6,7,8,9,10,11,12,13,14,15,16,17,18];row.values.forEach((v,i)=>assert.equal(v,rows[row.sourceRow-1][cols[i]-1]??null));}});
