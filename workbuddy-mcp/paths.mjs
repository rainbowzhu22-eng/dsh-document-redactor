import { fileURLToPath } from 'node:url';
import { dirname, join, isAbsolute } from 'node:path';
import { homedir } from 'node:os';
import { existsSync } from 'node:fs';

export const ADAPTER = dirname(fileURLToPath(import.meta.url));
const releasedPackage = join(ADAPTER, '../release-prep/release-1.5.0-dev.4/public-tree/001-contract-desensitizer/04-dsh-plugin');
const repositoryPackage = join(ADAPTER, '../001-contract-desensitizer/04-dsh-plugin');
// The Git distribution uses the existing released engine in the repository.
// Both locations are fixed layouts; MCP requests cannot choose an engine path.
export const PACKAGE = existsSync(join(releasedPackage, 'client.js')) ? releasedPackage : repositoryPackage;
// Distribution installers set a dedicated data home; existing pilot stays put.
const configuredHome = process.env.WORKBUDDY_REDACTOR_HOME;
if (configuredHome && !isAbsolute(configuredHome)) throw Error('INVALID_DATA_HOME');
export const HOME = configuredHome || join(homedir(), 'Library/Application Support/WorkBuddy Redaction Pilot');
export const ROOT = join(HOME, 'workbench-data');
