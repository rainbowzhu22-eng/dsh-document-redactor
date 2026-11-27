import { randomUUID } from 'node:crypto';

export class RulesBroker {
  constructor() { this.pending = null; }

  request(prompt, systemPrompt, signal) {
    if (this.pending) return Promise.reject(Error('RULES_BUSY'));
    return new Promise((resolve, reject) => {
      const id = randomUUID();
      const cancel = () => {
        if (this.pending?.id === id) this.pending = null;
        reject(Object.assign(Error('本次规则方案已取消或超时。'), { code: 'RULES_CANCELLED' }));
      };
      this.pending = { id, prompt, systemPrompt, resolve, signal, cancel };
      signal.addEventListener('abort', cancel, { once: true });
      if (signal.aborted) cancel();
    });
  }

  context() {
    const job = this.pending;
    if (!job) return { stage: 'idle' };
    return { stage: 'waiting_for_workbuddy', id: job.id,
      prompt: job.prompt, systemPrompt: job.systemPrompt.replace('禁止调用工具。', '只依据本次返回的数据生成方案；提交方案仅使用 redaction_rules_submit，不使用文件、终端、网络或其他工具。') };
  }

  submit(id, plan) {
    const job = this.pending;
    if (!job || job.id !== id || job.signal.aborted) throw Error('NO_PENDING_PLAN');
    if (!plan || typeof plan.summary !== 'string' || plan.summary.length > 1000 ||
        !Array.isArray(plan.edits) || plan.edits.length > 12 ||
        Object.keys(plan).some(key => !['summary','edits'].includes(key)) ||
        plan.edits.some(edit => !edit || Object.keys(edit).sort().join(',') !== 'after,before' ||
          typeof edit.before !== 'string' || typeof edit.after !== 'string') ||
        Buffer.byteLength(JSON.stringify(plan)) > 131072) throw Error('INVALID_PLAN');
    this.pending = null;
    job.signal.removeEventListener('abort', job.cancel);
    job.resolve(JSON.stringify(plan));
    return { stage: 'validating_local_plan' };
  }
}
