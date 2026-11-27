#!/usr/bin/env node
import { homedir } from 'node:os';
import { join } from 'node:path';

if (process.platform !== 'darwin' || Number(process.versions.node.split('.')[0]) < 22) {
  process.stderr.write('This beta requires macOS and Node.js 22 or later.\n');
  process.exitCode = 1;
} else {
  process.env.WORKBUDDY_REDACTOR_HOME ||= join(homedir(), 'Library/Application Support/WorkBuddy Document Redactor Beta/data');
  try {
    const { main } = await import('./workbench-mcp.mjs');
    await main();
  } catch {
    process.stderr.write('Local document workbench could not start.\n');
    process.exitCode = 1;
  }
}
