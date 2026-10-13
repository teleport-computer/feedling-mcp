import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const scratch = mkdtempSync(path.join(tmpdir(), 'mdx-shutdown-'));
const sha = (s) => createHash('sha256').update(s).digest('hex');
const source = () => readFileSync(path.join(root, '.source/server.ts'), 'utf8');
// Import identifiers may be allocated in different order between real emits.
// Compare actual exported ASTs with imported identifiers resolved to modules.
function exportsOf(text) {
  const file = ts.createSourceFile('server.ts', text, ts.ScriptTarget.Latest, true);
  const imports = new Map();
  for (const node of file.statements) {
    if (!ts.isImportDeclaration(node) || !node.importClause) continue;
    const mod = node.moduleSpecifier.text;
    const clause = node.importClause;
    if (clause.name) imports.set(clause.name.text, `${mod}:default`);
    if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings))
      imports.set(clause.namedBindings.name.text, `${mod}:*`);
    if (clause.namedBindings && ts.isNamedImports(clause.namedBindings))
      for (const item of clause.namedBindings.elements)
        imports.set(item.name.text, `${mod}:${item.propertyName?.text ?? item.name.text}`);
  }
  const canonical = (node) => {
    if (ts.isIdentifier(node)) return imports.get(node.text) ?? node.text;
    if (ts.isObjectLiteralExpression(node)) return node.properties.map(canonical).sort();
    // Compare semantic children rather than formatting or generated aliases.
    const semantic = []; ts.forEachChild(node, child => { semantic.push(canonical(child)); });
    return [ts.SyntaxKind[node.kind], semantic.length ? semantic : node.getText(file)];
  };
  return file.statements.filter(n => n.modifiers?.some(m => m.kind === ts.SyntaxKind.ExportKeyword)).map(canonical);
}
function run(label, command, args) {
  const trace = path.join(scratch, `${label}.jsonl`);
  const env = {...process.env,
    PATH: `${path.join(root, 'node_modules/.bin')}${path.delimiter}${process.env.PATH}`,
    NODE_OPTIONS: `--require=${JSON.stringify(path.join(root, 'scripts/delay-mdx-write.cjs'))}`,
    MDX_SHUTDOWN_TRACE: trace};
  delete env._FUMADOCS_MDX;
  const result = spawnSync(command, args, {cwd: root, env, encoding: 'utf8', timeout: 90000});
  if (result.error) throw result.error;
  const events = readFileSync(trace, 'utf8').trim().split('\n').map(JSON.parse);
  const generated = events.find(e => e.event === 'generated');
  assert.ok(generated?.bytes > 0, `${label}: real generation missing`);
  assert.deepEqual(events.map(e => e.event), ['generated', 'truncated', 'exit']);
  assert.equal(events[1].bytes, 0);
  assert.equal(events[2].code, 0);
  assert.equal(events[2].pending, true);
  assert.equal(events[2].bytes, 0);
  console.log(JSON.stringify({label, exit: result.status, generatedBytes: generated.bytes,
    generatedSha256: generated.sha256, finalBytes: Buffer.byteLength(source()), finalSha256: sha(source())}));
  return {...result, generated};
}
try {
  const old = run('old-order', '/bin/sh', ['-c', 'fumadocs-mdx && next typegen && tsc --noEmit']);
  assert.equal(old.status, 2, old.stdout + old.stderr);
  assert.match(old.stdout + old.stderr, /TS2306.*\.source\/server\.ts.*not a module/);
  assert.equal(source(), '');
  // Exercise the actual package script, so removing/reordering the final
  // awaited generator makes this regression red, without a string-only test.
  const fixed = run('package-script', 'npm', ['run', 'types:check']);
  assert.equal(fixed.status, 0, fixed.stdout + fixed.stderr);
  const actual = exportsOf(source());
  assert.ok(actual.length > 0, 'Genuine exports missing');
  assert.deepEqual(actual, exportsOf(fixed.generated.source));
  assert.deepEqual(actual, exportsOf(old.generated.source));
  console.log('PASS: old order TS2306; package script passes with identical genuine exports');
} finally {
  rmSync(scratch, {recursive: true, force: true});
}
