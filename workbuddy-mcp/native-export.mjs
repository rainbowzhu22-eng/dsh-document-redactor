import { mkdir, realpath, lstat, copyFile, chmod } from 'node:fs/promises';
import { constants } from 'node:fs';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';

const aliasPattern=/^[a-f0-9]{32}\.(docx|pdf|txt|md|xlsx|json)$/;
export async function saveExport(root, sid, name) {
  root=await realpath(root);
  if(!/^[a-f0-9]{12}$/.test(sid)||typeof name!=='string'||/[\\/\x00\r\n]/.test(name)||!/^.+\.(docx|pdf|txt|md|xlsx|json)$/i.test(name))throw Error('INVALID_EXPORT');
  const sessions=await realpath(join(root,'sessions'));
  const source=join(sessions,sid,'out',name), actual=await realpath(source);
  if(actual!==source||!actual.startsWith(sessions+'/')||!(await lstat(source)).isFile())throw Error('INVALID_EXPORT');
  const folder=join(root,'exports');await mkdir(folder,{recursive:true,mode:0o700});
  if(await realpath(folder)!==folder)throw Error('INVALID_EXPORT');
  const alias=randomBytes(16).toString('hex')+'.'+name.split('.').pop().toLowerCase();
  const target=join(folder,alias);await copyFile(source,target,constants.COPYFILE_EXCL);await chmod(target,0o600);
  return {alias,path:target};
}
export async function openExport(root,alias) {
  root=await realpath(root);
  if(typeof alias!=='string'||!aliasPattern.test(alias))throw Error('INVALID_EXPORT');
  const target=join(root,'exports',alias);
  if(await realpath(target)!==target||!(await lstat(target)).isFile())throw Error('INVALID_EXPORT');
  await new Promise((resolve,reject)=>{const child=spawn('/usr/bin/open',[target],{stdio:'ignore'});child.once('error',reject);child.once('exit',code=>code===0?resolve():reject(Error('OPEN_FAILED')));});
  return {opened:true};
}
