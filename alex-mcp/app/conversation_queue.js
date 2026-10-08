'use strict';

/**
 * Small in-process FIFO keyed by WhatsApp conversation.
 *
 * Tasks for one conversation run strictly in enqueue order, while different
 * conversations can run concurrently. A rejected task never poisons the lane.
 */
export class ConversationQueue {
  constructor() {
    this.tails = new Map();
  }

  enqueue(key, task) {
    const lane = String(key || '__unknown__');
    const previous = this.tails.get(lane) || Promise.resolve();
    const run = previous.catch(() => undefined).then(() => task());
    let tail;
    tail = run.finally(() => {
      if (this.tails.get(lane) === tail) this.tails.delete(lane);
    });
    this.tails.set(lane, tail);
    return run;
  }

  pendingLanes() {
    return this.tails.size;
  }
}

export function conversationKeyFromMessage(message) {
  return String(
    message && message.key && message.key.remoteJid
      ? message.key.remoteJid
      : '__unknown__'
  );
}
