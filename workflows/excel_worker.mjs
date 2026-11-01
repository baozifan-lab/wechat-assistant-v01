import fs from 'node:fs/promises';
import path from 'node:path';
import { FileBlob, SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const canonical=['业务编号','日期','部门','客户','金额'];
const aliases={'单号':'业务编号','业务日期':'日期','团队':'部门','客户名称':'客户','销售额':'金额'};
const clean=v=>typeof v==='string'?v.trim():v;
const literal=v=>typeof v==='string'&&v.startsWith('=')?"'"+v:v;

function dateValue(value){
  let s;
  if(value instanceof Date)s=value.toISOString().slice(0,10);
  else if(typeof value==='number'&&Number.isFinite(value))s=new Date(Date.UTC(1899,11,30)+Math.round(value)*86400000).toISOString().slice(0,10);
  else s=String(value??'').trim().replaceAll('/','-');
  const match=/^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s);
  if(!match)throw Error('日期格式无效');
  const [,year,month,day]=match.map(Number),d=new Date(Date.UTC(year,month-1,day));
  if(d.getUTCFullYear()!==year||d.getUTCMonth()!==month-1||d.getUTCDate()!==day)throw Error('日期不存在');
  return d.toISOString().slice(0,10);
}
function money(value){
  if(typeof value==='number'){
    if(!Number.isFinite(value)||Math.abs(value*100-Math.round(value*100))>0.00001)throw Error('金额须为有限数字且最多两位小数');
    return Math.round(value*100);
  }
  const s=String(value??'').trim();
  if(!/^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$/.test(s))throw Error('金额格式无效');
  return Math.round(Number(s.replaceAll(',',''))*100);
}
function format(sheet,range,headerRow,lastRow){
  const grid=sheet.getRange(range);
  grid.format.font={name:'Arial',size:11,color:'#20362C'};
  grid.format.rowHeight=24;
  grid.format.verticalAlignment='center';
  grid.format.columnWidth=18;
  sheet.showGridLines=false;
  sheet.getRange(`A${headerRow}:G${headerRow}`).format={fill:'#235F44',font:{bold:true,color:'#FFFFFF',name:'Arial',size:11},rowHeight:28,horizontalAlignment:'center'};
  sheet.getRange(`B${headerRow+1}:B${lastRow}`).setNumberFormat('yyyy-mm-dd');
  sheet.getRange(`E${headerRow+1}:E${lastRow}`).setNumberFormat('#,##0.00');
  sheet.getRange(`E${headerRow+1}:E${lastRow}`).format.horizontalAlignment='right';
  sheet.getRange(`G${headerRow+1}:G${lastRow}`).format.horizontalAlignment='right';
  sheet.getRange('F:F').format.columnWidth=28;
  sheet.getRange('G:G').format.columnWidth=12;
  sheet.freezePanes.freezeRows(headerRow);
}
async function exportBook(wb,file,preview,sheet,range){
  wb.recalculate();
  const inspection=await wb.inspect({kind:'table',range:`${sheet}!${range}`,include:'values,formulas',maxChars:3500,tableMaxRows:15,tableMaxCols:7});
  const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',options:{useRegex:true,maxResults:20},maxChars:1500});
  await fs.mkdir(path.dirname(file),{recursive:true});
  await (await SpreadsheetFile.exportXlsx(wb)).save(file);
  const picture=await wb.render({sheetName:sheet,range,scale:1.5,format:'png'});
  await fs.writeFile(preview,new Uint8Array(await picture.arrayBuffer()));
  await fs.writeFile(file+'.inspection.json',JSON.stringify({table:inspection.ndjson,errors:errors.ndjson}));
}
async function demo(root,selected){
  const day='2026-11-01';
  const inputs=[
    {name:'部门报表甲_示例.xlsx',headers:canonical,rows:[
      ['DEMO-001',day,'销售一部','示例客户甲',1200.50],
      ['DEMO-002',day,'销售二部','示例客户乙',800],
      ['DEMO-003',day,'销售一部','示例客户丙',0],
      ['DEMO-004',day,'销售一部','示例客户丁',650.25],
    ]},
    {name:'部门报表乙_示例.xlsx',headers:['单号','业务日期','团队','客户名称','销售额'],rows:[
      ['DEMO-002','2026/11/01','销售二部','示例客户乙','800.00'],
      ['DEMO-005','2026/11/01','销售二部','示例客户戊','2,000.00'],
      ['DEMO-006','2026/11/01','销售二部','示例客户己','350.00'],
      ['DEMO-007','2026/11/01','销售二部','','500.00'],
    ]},
  ];
  for(const source of inputs){
    if(selected&&source.name!==selected)continue;
    const wb=Workbook.create(),sheet=wb.worksheets.add('明细');
    sheet.getRange('A1:E5').values=[source.headers,...source.rows];
    sheet.getRange('A1:E5').format.font={name:'Arial',size:11};
    sheet.getRange('A1:E5').format.columnWidth=20;
    sheet.getRange('A1:E5').format.rowHeight=25;
    sheet.getRange('A1:E5').format.horizontalAlignment='left';
    sheet.getRange('A1:E1').format={fill:'#235F44',font:{name:'Arial',size:11,bold:true,color:'#FFFFFF'}};
    sheet.getRange('E2:E5').setNumberFormat('#,##0.00');
    sheet.getRange('E2:E5').format.horizontalAlignment='right';
    sheet.showGridLines=false;
    await exportBook(wb,path.join(root,'待处理',source.name),path.join(root,'样例预览',source.name+'.png'),'明细','A1:E5');
  }
  console.log(JSON.stringify({created:2,demo:true}));
}
async function merge(spec){
  const kept=[],issues=[],seen=new Map();let inputRows=0,duplicateRows=0,invalidRows=0;
  const files=[];
  for(const input of spec.inputs){
    const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(input.path));
    await wb.inspect({kind:'sheet',include:'id,name',maxChars:1200});
    const sheet=wb.worksheets.getItemAt(0),range=sheet.getUsedRange();
    const values=range.values,formulas=range.formulas;
    if(values.length>10001)throw Error('单份文件超过 10000 行限制');
    const headers=values[0].map(v=>aliases[String(v??'').trim()]||String(v??'').trim());
    const indexes=canonical.map(h=>headers.indexOf(h));
    if(indexes.some(i=>i<0)||new Set(headers).size!==headers.length)throw Error(`${input.name} 缺少必需字段或表头重复`);
    let fileRows=0;
    for(let i=1;i<values.length;i++){
      if(values[i].every(v=>v===null||v===undefined||v===''))continue;
      inputRows++;fileRows++;
      const source={source:input.name,sourceRow:i+1};
      try{
        if(formulas[i]?.some(v=>v))throw Error('首版仅处理明细值，含公式的源行需先明确处理规则');
        const row=indexes.map(index=>clean(values[i][index]));
        if(canonical.some((h,index)=>row[index]===null||row[index]===undefined||row[index]===''))throw Error('必填字段为空');
        row[0]=String(row[0]);row[1]=dateValue(row[1]);row[2]=String(row[2]);row[3]=String(row[3]);row[4]=money(row[4]);
        const old=seen.get(row[0]);
        if(old){
          if(JSON.stringify(old)!==JSON.stringify(row))throw Error(`业务编号 ${row[0]} 存在内容冲突`);
          duplicateRows++;issues.push({...source,kind:'完全重复',id:row[0],reason:'与已保留记录一致，按文档规则去重'});continue;
        }
        seen.set(row[0],row);kept.push({record:row,...source});
      }catch(error){
        if(String(error.message).includes('内容冲突'))throw error;
        invalidRows++;issues.push({...source,kind:'异常隔离',id:String(values[i][indexes[0]]??''),reason:error.message});
      }
    }
    files.push({name:input.name,rows:fileRows});
  }
  if(!kept.length)throw Error('没有可交付的有效记录');
  if(inputRows!==kept.length+duplicateRows+invalidRows)throw Error('输入行数去向不平衡');
  const totalCents=kept.reduce((sum,v)=>sum+v.record[4],0),wb=Workbook.create(),sheet=wb.worksheets.add('整合结果');
  const last=6+kept.length,totalRow=last+2;
  const output=kept.map(v=>[literal(v.record[0]),new Date(v.record[1]+'T00:00:00Z'),literal(v.record[2]),literal(v.record[3]),v.record[4]/100,literal(v.source),v.sourceRow]);
  sheet.getRange('A2').values=[['今日报表整合（示例）']];
  sheet.getRange('A2').format.font={name:'Arial',size:16,bold:true};
  sheet.getRange('A3').values=[['示例数据，仅用于验证流程']];
  sheet.getRange('A4').values=[[`输入 ${files.length} 份，原始 ${inputRows} 行，保留 ${kept.length} 行，去重 ${duplicateRows} 行，隔离 ${invalidRows} 行`]];
  sheet.getRange('A6:G6').values=[[...canonical,'来源文件','来源行']];
  sheet.getRange(`A7:G${last}`).values=output;
  sheet.getRange(`D${totalRow}`).values=[['金额合计']];
  sheet.getRange(`E${totalRow}`).formulas=[[`=SUM(E7:E${last})`]];
  format(sheet,`A6:G${totalRow}`,6,totalRow);
  sheet.getRange(`D${totalRow}:E${totalRow}`).format.font.bold=true;
  wb.recalculate();
  const calculated=sheet.getRange(`E${totalRow}`).values[0][0];
  if(Math.round(Number(calculated)*100)!==totalCents)throw Error('输出金额合计校验失败');
  await exportBook(wb,spec.output,path.join(path.dirname(spec.output),'预览.png'),'整合结果',`A1:G${totalRow}`);
  const reopened=await SpreadsheetFile.importXlsx(await FileBlob.load(spec.output));
  if(reopened.worksheets.getItemAt(0).getRange(`A7:A${last}`).values.length!==kept.length)throw Error('导出后行数校验失败');
  const report={demo:true,skill:'excel.merge.v1',input_files:files,input_rows:inputRows,output_rows:kept.length,duplicate_rows:duplicateRows,invalid_rows:invalidRows,total_amount:totalCents/100,issues,reconciliation_passed:true};
  await fs.writeFile(path.join(path.dirname(spec.output),'检查结果.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
}

if(process.argv[2]==='--demo')await demo(process.argv[3],process.argv[4]);
else if(process.argv[2]==='--job')await merge(JSON.parse(await fs.readFile(process.argv[3],'utf8')));
else throw Error('Use --demo <directory> or --job <spec.json>');
