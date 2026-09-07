import { Crepe } from '@milkdown/crepe';
import '@milkdown/crepe/theme/common/style.css';
import '@milkdown/crepe/theme/frame-dark.css';
import { splitFrontmatter, recombineFrontmatter, wordCount, readingMinutes, SerializedSaveQueue } from './editor-helpers.mjs';

class CoreWriter {
  constructor() { this.crepe = null; this.root = null; this.change = () => {}; this.markdown = ''; }

  async mount(root, markdown = '', onChange = () => {}) {
    await this.destroy();
    this.root = root;
    this.change = onChange;
    this.markdown = markdown;
    this.crepe = new Crepe({ root, defaultValue: markdown, features: {
      [Crepe.Feature.Latex]: false,
      [Crepe.Feature.AI]: false,
      [Crepe.Feature.ImageBlock]: false,
    }});
    await this.crepe.create();
    this.crepe.on((listener) => listener.markdownUpdated((_ctx, value) => {
      this.markdown = value;
      this.change(value);
    }));
  }

  getMarkdown() { return this.crepe ? this.crepe.getMarkdown() : this.markdown; }

  async setMarkdown(markdown) {
    this.markdown = String(markdown ?? '');
    if (!this.crepe) return;
    // Crepe exposes the canonical Markdown serializer; recreate only the document
    // when loading a different file so no ProseMirror DOM is mutated manually.
    const root = this.root;
    const callback = this.change;
    await this.mount(root, this.markdown, callback);
  }

  focus() { this.root?.querySelector('[contenteditable="true"]')?.focus(); }
  setReadonly(value) { this.crepe?.setReadonly(Boolean(value)); }
  onChange(callback) { this.change = callback; }
  async destroy() { if (this.crepe) { await this.crepe.destroy(); this.crepe = null; } }
  insertMarkdown(markdown) { this.markdown = `${this.getMarkdown()}${markdown}`; return this.setMarkdown(this.markdown); }
  insertImage({ url, alt = '' }) { return this.insertMarkdown(`\n![${alt}](${url})\n`); }
}

window.CoreWriter = CoreWriter;
window.CoreWriterHelpers = { splitFrontmatter, recombineFrontmatter, wordCount, readingMinutes, SerializedSaveQueue };
