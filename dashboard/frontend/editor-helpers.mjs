/** Pure helpers shared by the Writer and its no-browser tests. */
export function splitFrontmatter(markdown = '') {
  const value = String(markdown);
  if (!value.startsWith('---')) return { frontmatter: '', body: value };
  const openingEnd = value.match(/^---(?:\r?\n)/);
  if (!openingEnd) return { frontmatter: '', body: value };
  const close = value.indexOf('\n---', openingEnd[0].length);
  if (close < 0) return { frontmatter: '', body: value };
  let end = close + 4;
  if (value[end] === '\r') end += 1;
  if (value[end] === '\n') end += 1;
  return { frontmatter: value.slice(0, end), body: value.slice(end) };
}

export function recombineFrontmatter(frontmatter, body = '') {
  return frontmatter ? `${frontmatter}${body}` : String(body);
}

export function wordCount(markdown = '') {
  const text = splitFrontmatter(markdown).body
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[[^\]]*\]\([^)]*\)/g, '$1')
    .replace(/[#>*_`|~-]/g, ' ');
  return text.trim() ? text.trim().split(/\s+/u).length : 0;
}

export function readingMinutes(markdown, wordsPerMinute = 225) {
  const words = wordCount(markdown);
  return words ? Math.max(1, Math.ceil(words / wordsPerMinute)) : 0;
}

export class SerializedSaveQueue {
  constructor(save, { delay = 1200, onState = () => {} } = {}) {
    this.save = save;
    this.delay = delay;
    this.onState = onState;
    this.timer = null;
    this.pending = null;
    this.running = false;
    this.sequence = 0;
  }

  schedule(value) {
    this.pending = { value, sequence: ++this.sequence };
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), this.delay);
    this.onState('unsaved');
  }

  async flush() {
    clearTimeout(this.timer);
    if (this.running || !this.pending) return;
    this.running = true;
    const item = this.pending;
    this.pending = null;
    this.onState('saving');
    try {
      await this.save(item.value);
      if (this.pending) this.onState('unsaved');
      else this.onState('saved');
    } catch (error) {
      this.pending = this.pending || item;
      this.onState('error', error);
      throw error;
    } finally {
      this.running = false;
      if (this.pending && !this.timer) this.timer = setTimeout(() => this.flush(), this.delay);
    }
  }

  cancel() {
    clearTimeout(this.timer);
    this.timer = null;
  }
}
