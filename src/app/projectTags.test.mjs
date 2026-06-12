import test from 'node:test';
import assert from 'node:assert/strict';
import { projectTagClass } from './projectTags.js';

test('project tag class is deterministic for a project title', () => {
  assert.equal(projectTagClass('Epsilon'), projectTagClass('Epsilon'));
});

test('project tag class returns the existing session-list palette classes', () => {
  assert.equal(projectTagClass('Epsilon'), 'border-violet-200 bg-violet-100 text-violet-800');
});
