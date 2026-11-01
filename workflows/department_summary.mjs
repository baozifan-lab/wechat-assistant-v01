// Registered deterministic skill: read A's exported workbook, summarize by department.
import fs from 'node:fs/promises';
import path from 'node:path';
import {FileBlob, SpreadsheetFile} from '@oai/artifact-tool';

const spec=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const check=JSON.parse(await fs.readFile(spec.check,'utf8'));
const workbook=await SpreadsheetFile.importXlsx(await FileBlob.load(spec.input));
const values=workbook.worksheets.getItemAt(0).getUsedRange().values;
const headers=['业务编号','日期','部门','客户','金额','来源文件','来源行'];
// Imported used ranges can start at A2; find the single exact header row.
const headerRows=values.map((row,index)=>headers.every((name,col)=>row[col]===name)?index:-1).filter(index=>index>=0);
if(headerRows.length!==1)throw Error('第一段输出格式与汇总技能不一致');
const departments=new Map(),seen=new Set();let totalCents=0,rows=0,zeroRows=0;
for(const row of values.slice(headerRows[0]+1)){
  if(row[0]===null||row[0]===undefined||row[0]==='')continue;
  if(seen.has(row[0]))throw Error('交接表中仍有重复业务编号');
  if(typeof row[4]!=='number'||!Number.isFinite(row[4])||!row[2])throw Error('交接数据缺少有效部门或金额');
  const cents=Math.round(row[4]*100);
  if(!Number.isSafeInteger(cents))throw Error('金额超出安全计算范围');
  seen.add(row[0]);rows++;totalCents+=cents;if(cents===0)zeroRows++;
  const department=String(row[2]);
  const group=departments.get(department)||{department,rows:0,cents:0};
  group.rows++;group.cents+=cents;departments.set(department,group);
}
if(rows!==check.output_rows||totalCents!==Math.round(check.total_amount*100))throw Error('与 A 的行数或合计不一致');
const groups=[...departments.values()].sort((a,b)=>a.department.localeCompare(b.department,'zh-CN'))
  .map(g=>({department:g.department,rows:g.rows,total_amount:g.cents/100}));
const report={demo:true,model_used:false,skill:'department.summary.v1',upstream_code:spec.code,
  upstream_version:spec.version,rows,zero_amount_rows:zeroRows,total_amount:totalCents/100,departments:groups};
const safe=value=>String(value).replaceAll('|','／').replace(/[\r\n]/g,' ');
const table=groups.map(g=>`| ${safe(g.department)} | ${g.rows} | ${g.total_amount.toFixed(2)} |`).join('\n');
const text=`# 第二段：模拟 B 部门汇总\n\n来源：${spec.code} V${spec.version}，A 已确认的 Excel。\n\n固定技能执行，未调用大模型。以下仅示例数据。\n\n| 部门 | 有效记录 | 金额合计 |\n|---|---:|---:|\n${table}\n\n共 ${rows} 条有效记录，合计 ${(totalCents/100).toFixed(2)}；保留零金额 ${zeroRows} 条。\n\nA 已去重 ${check.duplicate_rows} 行、隔离异常 ${check.invalid_rows} 行，B 仅分析交接后的有效记录。\n\n核对后回复：确认第二段 ${spec.code} V${spec.version}\n`;
await fs.mkdir(spec.output,{recursive:true});
await fs.writeFile(path.join(spec.output,'部门汇总.json'),JSON.stringify(report,null,2));
await fs.writeFile(path.join(spec.output,'部门汇总.md'),text);
console.log(JSON.stringify(report));
