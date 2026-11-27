import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { PACKAGE } from './paths.mjs';

const [service, rules, routes, runtime] = await Promise.all(
  ['service.js', 'rules-agent.js', 'index.js', 'runtime.js'].map(
    name => import(pathToFileURL(join(PACKAGE, name)).href),
  ),
);

export const RedactorService = service.RedactorService;
export const RulesController = rules.RulesController;
export const runRulesAgent = rules.runRulesAgent;
export const makeRoute = routes.makeRoute;
export const LocalRuntime = runtime.LocalRuntime;
