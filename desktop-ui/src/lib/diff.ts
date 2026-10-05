export interface DiffLine { t: 'add' | 'del' | 'ctx' | 'hunk' | 'note'; text: string; a?: number; b?: number }
export interface DiffFile {
  path: string;
  status: 'modified' | 'added' | 'deleted' | 'renamed' | 'binary';
  add: number;
  del: number;
  lines: DiffLine[];
}

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let inHunk = false;
  let a = 0;
  let b = 0;

  for (const raw of text.split('\n')) {
    if (raw.startsWith('diff --git ')) {
      const m = raw.match(/^diff --git a\/(.*) b\/(.*)$/);
      file = { path: m ? m[2] : raw.slice(11), status: 'modified', add: 0, del: 0, lines: [] };
      files.push(file);
      inHunk = false;
      continue;
    }
    if (!file) continue;

    if (!inHunk) {
      if (raw.startsWith('new file mode')) file.status = 'added';
      else if (raw.startsWith('deleted file mode')) file.status = 'deleted';
      else if (raw.startsWith('rename to ')) { file.status = 'renamed'; file.path = raw.slice(10); }
      else if (raw.startsWith('Binary files') || raw.startsWith('GIT binary patch')) file.status = 'binary';
    }
    const hunk = raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
    if (hunk) {
      inHunk = true;
      a = +hunk[1];
      b = +hunk[2];
      file.lines.push({ t: 'hunk', text: raw });
      continue;
    }
    if (!inHunk) continue; // header lines (index, ---, +++) before the first hunk
    if (raw.startsWith('+')) { file.add++; file.lines.push({ t: 'add', text: raw.slice(1), b: b++ }); }
    else if (raw.startsWith('-')) { file.del++; file.lines.push({ t: 'del', text: raw.slice(1), a: a++ }); }
    else if (raw.startsWith('\\')) file.lines.push({ t: 'note', text: raw });
    else if (raw.length || raw === ' ') file.lines.push({ t: 'ctx', text: raw.slice(1), a: a++, b: b++ });
  }
  return files;
}
