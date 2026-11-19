import {readSheet} from 'read-excel-file/browser';
import {SHEET,parseSummary} from './portfolio-summary';
export async function readWorkbook(file:File){
  const rows=await readSheet(file,SHEET);
  let date;
  try{const update=await readSheet(file,'UPDATE DATE');if(String(update[8]?.[3]??'').trim().toLowerCase()==='update date')date=update[8]?.[4];}catch{/* The reporting date is optional. */}
  return parseSummary(rows,file.name,date);
}
