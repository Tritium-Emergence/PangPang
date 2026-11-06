// Card title and the download attribute both use artifactFileName.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {artifactFileName} from '../web-src/pure.ts';

const win = 'C:\\Users\\' + 'verylongdirectoryname\\'.repeat(8) + 'final-report.docx';

test('a Windows stored name without rel is the filename, not the full path', () => {
  assert.ok(win.length > 120);
  const shown = artifactFileName({name: win, path: win});
  assert.equal(shown, 'final-report.docx');
  assert.equal(shown.includes('C:'), false);
  assert.equal(shown.includes('verylongdirectoryname'), false);
  // The card heading and <a download> are this same string.
  assert.equal(artifactFileName({path: win}), 'final-report.docx');
});

test('a contract name is not rewritten', () => {
  assert.equal(artifactFileName({rel: 'report.md', path: '/out/samples/actual.md', name: 'actual.md'}), 'report.md');
  assert.equal(artifactFileName({rel: 'sub\\final.md', name: 'sub\\final.md'}), 'sub\\final.md');
  assert.equal(artifactFileName({rel: 'dir/sub\\final.md'}), 'sub\\final.md');
});
