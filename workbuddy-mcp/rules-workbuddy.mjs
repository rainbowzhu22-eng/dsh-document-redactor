import { runRulesAgent } from './engine.mjs';

// Reuse the released rule prompt and one-turn orchestration.
export function createRulesGenerator(invoke) {
  return async (text, request, signal) => {
    const ctx = {
      agentDefaultModel: { currentSelection: () => ({ model: 'WorkBuddy auto' }) },
      agents: {
        async create(options) {
          const hooks = new Map();
          let message;
          const agent = {
            session: { append() {} },
            followup(value) { message = value; },
            cancel() {},
            async whenIdle() {
              const assembly = await hooks.get('system-prompt/assemble')(null, null, async () => ({}));
              await hooks.get('agent/pre-step')({ step: 1 }, async () => {});
              const answer = await invoke(
                message.content.map(item => item.text).join('\n'),
                assembly.sections.map(item => item.text).join('\n'), signal,
              );
              hooks.get('session/event')(agent.session, {
                type: 'assistant/message', data: { message: { content: [{ type: 'text', text: answer }] } },
              });
            },
          };
          options.setup({
            tools: {
              restrict({ allow }) { if (allow.length) throw Error('TOOLS_MUST_BE_DISABLED'); },
              guard() {},
            },
            on(name, handler) { hooks.set(name, handler); },
          }, agent);
          return { agent, async dispose() {} };
        },
      },
    };
    return runRulesAgent(ctx, text, request, signal);
  };
}
