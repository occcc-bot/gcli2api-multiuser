import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../front/users.html', import.meta.url), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check all portal JavaScript, not only the extracted helpers.

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; this.textContent = ''; }
  setAttribute(name, value) { this.attributes[name] = value; }
  set innerHTML(_) { throw new Error('Quota rendering must not use innerHTML'); }
  classList = { remove() {} };
}

const helpers = script.slice(script.indexOf('  function quotaResetTime('), script.indexOf('  async function loadCredentials('));
const context = { document: { createElement: tag => new Element(tag) } };
vm.createContext(context);
vm.runInContext(helpers, context);

function texts(node) { return [node.textContent, ...node.children.flatMap(texts)].join('\n'); }
function nodes(node) { return [node, ...node.children.flatMap(nodes)]; }

test('renders model names as text, percentages and accessible progress', () => {
  const panel = new Element();
  const model = '<img src=x onerror=alert(1)>';
  context.renderQuota(panel, { success: true, models: { [model]: { remaining: 0.375, resetTimeRaw: '2030-01-01T00:00:00Z' } } });
  assert.ok(texts(panel).includes(model));
  assert.ok(texts(panel).includes('剩余 37.5%'));
  const progress = nodes(panel).find(node => node.tag === 'progress');
  assert.equal(progress.value, 0.375);
  assert.equal(progress.attributes['aria-label'], model + ' 剩余额度');
});

test('empty models and unknown quota are explicit, rather than showing zero', () => {
  const panel = new Element();
  context.renderQuota(panel, { success: true, models: {} });
  assert.ok(texts(panel).includes('暂未返回'));
  context.renderQuota(panel, { success: true, models: { unknown: {} } });
  assert.ok(texts(panel).includes('剩余额度未知'));
  assert.ok(texts(panel).includes('重置时间：未知'));
  assert.equal(nodes(panel).filter(node => node.tag === 'progress').length, 0);
});

test('remaining progress is finite and clamped to 0–100%', () => {
  const panel = new Element();
  context.renderQuota(panel, { success: true, models: { negative: { remaining: -5 }, overflow: { remaining: 9 }, invalid: { remaining: NaN } } });
  assert.ok(texts(panel).includes('剩余 0%'));
  assert.ok(texts(panel).includes('剩余 100%'));
  assert.ok(texts(panel).includes('剩余额度未知'));
});

test('reset times use local timezone or label the upstream UTC+8 fallback', () => {
  assert.equal(context.quotaResetTime({ resetTimeRaw: '2030-01-01T00:00:00Z' }), new Date('2030-01-01T00:00:00Z').toLocaleString());
  assert.equal(context.quotaResetTime({ resetTimeRaw: 'invalid', resetTime: '01-01 08:00' }), '01-01 08:00（UTC+8）');
  assert.equal(context.quotaResetTime({ resetTime: 'N/A' }), '未知');
});

test('invalid quota payload is rejected', () => {
  for (const data of [{ success: false, models: {} }, { success: true }, { success: true, models: [] }]) {
    assert.throws(() => context.renderQuota(new Element(), data), /额度响应格式不正确/);
  }
});
