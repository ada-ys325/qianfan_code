# Third-party data notices

The Apache-2.0 [`LICENSE`](LICENSE) at the repository root covers this project's
own code. It does not, and cannot, relicense third-party material that ships
inside the development task fixtures. This file records where that material came
from and what still needs a licensing decision before a public release.

Status legend: **confirmed** means a maintainer has verified the redistribution
right; **unconfirmed** means nobody has yet, and the asset should be treated as
not cleared for public redistribution.

## Ministry of Education project listing (XLSX)

- Path: `dumatebench/datasets/dev/task_1/workspace_seed/uploads/` — one workbook
  exported from the platform, roughly 1,176 rows of project titles and detail
  URLs.
- Source: `https://www.crs.jsj.edu.cn/` (中华人民共和国教育部 中外合作办学监管工作信息平台),
  export dated 2026-05-29.
- Local description: `dumatebench/datasets/dev/task_1/web_reference/ref_14_官方项目名单Excel.md`.
- Redistribution right: **unconfirmed**. No licence or written permission is
  recorded for the export.

## Ministry of Education detail pages (HTML snapshots)

- Path: `dumatebench/datasets/dev/task_1/web_reference/assets/` — 13 full page
  snapshots, listed in `web_reference/validation_manifest.json`.
- Source: same platform as above.
- Redistribution right: **unconfirmed, with an explicit restriction on file.**
  The captured pages carry the site's own footer notice:

  ```text
  版权所有：教育部教育涉外监管信息网
  本网站由中国教育和科研计算机网制作维护，保留所有权利。未经允许不得复制、镜像。
  ```

  This is a stated prohibition on copying and mirroring, not merely an absent
  licence. Adding an Apache-2.0 header or a README attribution line does not
  grant the permission.

## Derived evaluator references

- Path: `dumatebench/datasets/dev/task_1/evaluator/gold_answer_reference.json`
  and the `web_reference/ref_*.md` summaries.
- These are derived from the two asset groups above, so their redistribution
  status follows whatever is decided for the sources.

## Browser session artefacts

- Path: `dumatebench/datasets/dev/task_1/workspace_seed/history_agent_files/`
  - `page-2026-05-29T05-51-10-485Z.yml` — an accessibility-tree capture of the
    same Ministry of Education pages.
  - `projects.json` — a scraped project list (region, type, title, detail URL).
  - `bash`, `env` — stray Linux ELF executables, ~1.4 MB and ~44 KB. These look
    like container binaries copied in by accident rather than task data. They
    have not been removed here because they sit inside a task's
    `workspace_seed/`, which is handed to the agent at runtime; deleting them
    could change task behaviour. A maintainer should confirm whether the task
    needs them and drop them if not.
- Redistribution and privacy review: **unconfirmed.** Session/history captures
  should be checked for anything identifying a real person or account before a
  public release.

## Hugging Face dataset card

- `README.md` links the packaged dataset at
  `https://huggingface.co/datasets/Annihi/dumate_bench`.
- The card is currently labelled `apache-2.0`. That label is only accurate for
  content this project may license; it does not extend to the assets above.

## Open decisions

1. Obtain, and record here, written permission for the Ministry of Education
   XLSX, HTML snapshots and derived gold answers — or remove them from public
   distribution.
2. If permission cannot be obtained, replace them with URLs, field schemas and
   content hashes, or with synthetic samples, and regenerate the evaluator
   references.
3. Decide whether `history_agent_files/bash` and `env` belong in the task at all.
4. Re-check the Hugging Face card and archive once 1–3 are settled.
