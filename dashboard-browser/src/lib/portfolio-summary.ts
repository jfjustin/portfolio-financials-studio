export type Cell = unknown;
export type Row = { name: string; sourceRow: number; values: (number | null)[] };
export type Summary = { direct: Row[]; portfolio: Row[]; irr: number[]; date: string | null; fundDate: string | null; filename: string };
export const SHEET = 'Summary-Portfolio Value';
const normalize = (v: unknown) => String(v ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
export function parseSummary(data: Cell[][], filename: string, updateDate?: Cell): Summary {
  const cell = (r: number, c: number) => data[r - 1]?.[c - 1];
  const expect = (r: number, c: number, label: string) => {
    if (normalize(cell(r, c)) !== normalize(label)) throw new Error(`Unexpected layout at ${String.fromCharCode(64+c)}${r}: expected “${label}”. Use the original summary layout.`);
  };
  const shared = [[5,'# of Company'],[7,'Total Value'],[8,'Book Value'],[9,'Impairment'],[11,'Realized Gain/(Loss)'],[12,'Unrealized Gain/(Loss)'],[13,'Total Gain/(Loss)'],[15,'TVPI'],[18,'% Capital Allocation']] as const;
  for (const r of [79,87]) for (const [c,label] of shared) expect(r,c,label);
  for (const [r,c,label] of [[79,6,'Investment cost'],[79,10,'Total Received Proceeds'],[79,14,'Gain/Loss (%)'],[87,6,'Total investment (Paid-in Capital)'],[87,10,'Proceeds & Distribution'],[87,14,'Gain/Loss'],[87,16,'DPI'],[87,17,'AUM'],[93,3,'IRR for Core Investments'],[94,3,'IRR for Life Science Investments']] as const) expect(r,c,label);
  const labels = ['Current Direct Investment','Impaired Investment','Exit Investment','Total Direct Investment','Fund Investment','Total Investments','Life Sciences Investments','Core Investments'];
  const rows = [80,81,82,83,85,88,89,90].map((r,i) => {
    expect(r,3,labels[i]);
    const columns = r < 87 ? [5,6,7,8,9,10,11,12,13,14,15,18] : [5,6,7,8,9,10,11,12,13,14,15,16,17,18];
    return {name: labels[i],sourceRow:r, values: columns.map(c => {
      const v=cell(r,c);
      if (v === null || v === undefined || v === '') {
        if (c === 18) return null;
        throw new Error(`Missing value at ${String.fromCharCode(64+c)}${r}. Save the workbook in Excel to recalculate formulas, then upload it again.`);
      }
      if (typeof v !== 'number' || !Number.isFinite(v)) throw new Error(`Invalid number at ${String.fromCharCode(64+c)}${r}. Check the formula and save the workbook in Excel.`);
      return v;
    })};
  });
  const irr = [93,94].map(r => { const v=cell(r,5);if(typeof v !== 'number'||!Number.isFinite(v))throw new Error(`Missing IRR at E${r}. Recalculate and save in Excel.`);return v; });
  let date: Date | null = updateDate instanceof Date ? updateDate : null;
  if (!date && typeof updateDate === 'number') date = new Date(Date.UTC(1899,11,30) + updateDate * 86400000);
  const formatted = date && Number.isFinite(date.getTime()) ? date.toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric',timeZone:'UTC'}) : null;
  const match = String(cell(5,12) ?? '').match(/Q([1-4])['’\s-]*(\d{2}|\d{4})\s*NAV/i);
  let fundDate=null;
  if(match){const y=match[2].length===2?2000+Number(match[2]):Number(match[2]); fundDate=new Date(Date.UTC(y,Number(match[1])*3,0)).toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric',timeZone:'UTC'});}
  return {direct:rows.slice(0,5),portfolio:rows.slice(5),irr,date:formatted,fundDate,filename};
}
export const money = (n: number | null) => n===null?'—':`${n<0?'-':''}$${Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:1})}M`;
export const percent = (n:number|null) => n===null?'—':`${(n*100).toFixed(1)}%`;
export const multiple = (n:number|null) => n===null?'—':`${n.toFixed(2)}x`;
export function display(n:number|null,index:number,portfolio=false){if(n===null)return '—';if(index===0)return String(n);if(index===9 || index===(portfolio?13:11))return percent(n);if(index===10 || (portfolio&&index===11))return multiple(n);return money(n);}
export function csvFor(s:Summary){const quote=(v:string)=>`"${v.replaceAll('"','""')}"`;return '\ufeff'+[['Investment types',...HEADERS_DIRECT],...s.direct.map(r=>[r.name,...r.values.map((n,i)=>display(n,i))]),[],['Portfolio categories',...HEADERS_PORTFOLIO],...s.portfolio.map(r=>[r.name,...r.values.map((n,i)=>display(n,i,true))]),[],['IRR Core',percent(s.irr[0])],['IRR Life Sciences',percent(s.irr[1])]].map(r=>r.map(quote).join(',')).join('\r\n');}
export const HEADERS_DIRECT=['Investments','Paid-in capital','Total value','Book value','Impairment','Proceeds received','Realized gain / loss','Unrealized gain / loss','Total gain / loss','Gain / loss %','TVPI','Capital allocation'];
export const HEADERS_PORTFOLIO=[...HEADERS_DIRECT.slice(0,11),'DPI','AUM','Capital allocation'];
