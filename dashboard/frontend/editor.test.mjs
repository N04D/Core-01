import test from 'node:test';
import assert from 'node:assert/strict';
import { splitFrontmatter, recombineFrontmatter, wordCount, readingMinutes, SerializedSaveQueue } from './editor-helpers.mjs';

test('frontmatter is split and recombined byte-for-byte', () => {
  const source = '---\ntopic: ""\ntitle: Example\ntags:\n  - één\n---\n\n# Hallo\n';
  const parts = splitFrontmatter(source);
  assert.equal(parts.frontmatter, source.slice(0, source.indexOf('\n\n# Hallo\n')) + '\n');
  assert.equal(recombineFrontmatter(parts.frontmatter, parts.body), source);
});

test('markdown without or with empty frontmatter remains valid', () => {
  for (const source of ['', '# Titel\n', '---\n---\n\nbody']) {
    const parts = splitFrontmatter(source);
    assert.equal(recombineFrontmatter(parts.frontmatter, parts.body), source);
  }
});

test('word count excludes frontmatter and reading time is deterministic', () => {
  assert.equal(wordCount('---\ntitle: hidden words\n---\n\nEen twee drie'), 3);
  assert.equal(readingMinutes('een '.repeat(226)), 2);
});

test('serialized saves keep newer edits dirty while an older save runs', async () => {
  let release;
  const states = [];
  const saved = [];
  const queue = new SerializedSaveQueue(async value => { saved.push(value); await new Promise(resolve => { release = resolve; }); }, { delay: 0, onState: state => states.push(state) });
  queue.schedule('one');
  await new Promise(resolve => setTimeout(resolve, 5));
  queue.schedule('two');
  release();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.deepEqual(saved, ['one', 'two']);
  assert.ok(states.includes('unsaved'));
});
