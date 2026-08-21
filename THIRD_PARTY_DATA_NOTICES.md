# Third-party data notices

The Apache-2.0 [`LICENSE`](LICENSE) at the repository root covers this project's
own code. It does not, and cannot, relicense third-party material that ships
inside the development task fixtures. This file records where that material came
from and what still needs a licensing decision before a public release.

Status legend: **confirmed** means a maintainer has verified the redistribution
right; **unconfirmed** means nobody has yet, and the asset should be treated as
not cleared for public redistribution.

## Ministry of Education project listing (XLSX)

- **Not distributed in this repository.** The workbook was removed before the
  public release; only its description below remains.
- Original path: `dumatebench/datasets/dev/task_1/workspace_seed/uploads/` — one
  workbook exported from the platform, roughly 1,176 rows of project titles and
  detail URLs.
- Source: `https://www.crs.jsj.edu.cn/` (中华人民共和国教育部 中外合作办学监管工作信息平台),
  export dated 2026-05-29.
- Local description: `dumatebench/datasets/dev/task_1/web_reference/ref_14_官方项目名单Excel.md`.
- Redistribution right: **unconfirmed.** No licence or written permission is
  recorded for the export, so it is not redistributed here.

## Ministry of Education detail pages (HTML snapshots)

- **Not distributed in this repository.** The 13 snapshots were removed before
  the public release. `web_reference/validation_manifest.json` still lists them
  with their source URLs and the `ref_*.md` summaries still cite them, so the
  derivation of the gold answers stays auditable and the pages can be re-fetched
  from the source by anyone who needs to verify them.
- Original path: `dumatebench/datasets/dev/task_1/web_reference/assets/`.
- Source: same platform as above.
- Redistribution right: **unconfirmed, with an explicit restriction on file.**
  The captured pages carry the site's own footer notice:

  ```text
  版权所有：教育部教育涉外监管信息网
  本网站由中国教育和科研计算机网制作维护，保留所有权利。未经允许不得复制、镜像。
  ```

  This is a stated prohibition on copying and mirroring, not merely an absent
  licence. Adding an Apache-2.0 header or a README attribution line does not
  grant the permission, which is why the files are not shipped.

## Derived evaluator references

- Path: `dumatebench/datasets/dev/task_1/evaluator/gold_answer_reference.json`
  and the `web_reference/ref_*.md` summaries.
- These are factual field extractions derived from the two asset groups above,
  not copies of the pages. They are retained because the task's evaluator reads
  the gold answers directly and cannot score the task without them. If the
  maintainer decides even the extracted values need clearing, the gold answers
  have to be regenerated from an approved source.

## Browser session artefacts

- Path: `dumatebench/datasets/dev/task_1/workspace_seed/history_agent_files/`
  - `page-2026-05-29T05-51-10-485Z.yml` — **not distributed in this repository.**
    An accessibility-tree capture of the same Ministry of Education pages, so it
    is the same page content in another format and follows the same decision as
    the HTML snapshots. A scan for cookies, tokens, credentials, e-mail
    addresses and phone numbers found nothing, so it was excluded on
    redistribution grounds rather than privacy grounds.
  - `projects.json` — a scraped project list (region, type, title, detail URL).
    Retained: it is the task's only declared input.
  - `bash`, `env` — stray Linux ELF executables, ~1.4 MB and ~44 KB. These look
    like container binaries copied in by accident rather than task data. They
    have not been removed because they sit inside a task's `workspace_seed/`,
    which is handed to the agent at runtime; deleting them could change task
    behaviour. A maintainer should confirm whether the task needs them and drop
    them if not.

## Hugging Face dataset card

- `README.md` links the packaged dataset at
  `https://huggingface.co/datasets/Annihi/dumate_bench`.
- The card is currently labelled `apache-2.0`. That label is only accurate for
  content this project may license; it does not extend to the assets above.

## Open decisions

1. The Ministry of Education XLSX and the 13 HTML snapshots are excluded from
   this repository pending written permission. If permission is obtained, record
   it here before adding them back.
2. The derived gold answers are still shipped. Decide whether the extracted field
   values also need clearing; if so, regenerate them from an approved source.
3. Decide whether `history_agent_files/bash` and `env` belong in the task at all,
   and review `page-2026-05-29T05-51-10-485Z.yml` for anything identifying a real
   person or account.
4. Re-check the Hugging Face card and archive, which are packaged separately from
   this repository and may still contain the excluded assets.
