"""Shared user-facing notice text for document review and export pages."""

EXPORT_REVIEW_NOTICE = (
    "自动识别无法穷尽文书中的全部敏感字段，识别结果也可能有偏差，"
    "外发前请人工核对导出的文件。"
)

EXPORT_UNRECOGNIZED_NOTE = "可能未识别的敏感内容将按原文保留。"


EXPORT_FEEDBACK_JS = r"""
async function requestExportResult(url, options, stage){
  let response;
  try{
    response=await fetch(url,options);
  }catch(error){
    return {ok:false,stage,code:'EXPORT_CONNECTION_LOST',error:'本机服务连接中断，未能确认本次操作结果。',
      advice:'请重新打开 DSH 的文书脱敏工作台，先查看历史项目是否已有产物，再决定是否重试。'};
  }
  let result;
  try{result=await response.json()}catch(error){
    return {ok:false,stage,code:'EXPORT_RESPONSE_INVALID',error:'本机服务返回了无法读取的结果。',
      advice:'先在历史项目查看产物；若没有，刷新复核页重新核对后再试。'};
  }
  if(!result||typeof result!=='object'||Array.isArray(result)){
    return {ok:false,stage,code:'EXPORT_RESPONSE_INVALID',error:'本机服务返回了无法读取的结果。',
      advice:'先在历史项目查看产物；若没有，刷新复核页重新核对后再试。'};
  }
  if(response.ok&&result.ok)return result;
  return {...result,ok:false,stage:result.stage||stage};
}
function showExportProblem(host, details){
  const message=document.createElement('strong');
  message.textContent=(details.stage?details.stage+' · ':'')+(details.error||'本次操作未完成。');
  host.replaceChildren(message);
  const location=details.location;
  if(location&&(location.part||(location.pages||[]).length)){
    const position=document.createElement('div');
    position.textContent=location.part
      ? [location.section,location.part,location.ordinal&&('第 '+location.ordinal+' 个'+location.kind)].filter(Boolean).join(' · ')
      : '第 '+location.pages.join('、')+' 页';
    host.append(position);
  }
  const advice=document.createElement('p');
  advice.textContent=details.advice||'核对当前项目和处理选择后重试。';
  host.append(advice);
  if(details.code){
    const code=document.createElement('small');
    code.textContent=details.code;
    host.append(code);
  }
}
"""
