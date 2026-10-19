# Semantic assurance: ограниченный контракт оценки

Этот документ определяет воспроизводимый контракт оценки локальной модели для
governed semantic editor. Он отделён от runtime и proposal queue: оценка не
меняет SQLite, Mem0, Qdrant, конфигурацию, модель, lifecycle или outbox.

## Что может и не может доказать прогон

Конечный набор примеров не может доказать универсальную factual accuracy,
entailment, relevance или отсутствие hidden conflicts. Даже 100% на таком
наборе — наблюдаемый результат для точной пары «модель + вариант + prompt +
dataset + runner», а не семантическая истина и не разрешение на automatic
semantic apply.

`automatic semantic apply` остаётся `NO-GO`. Текущий код требует
привилегированного human review с точной attestation и отдельный operator
apply с повторной SQLite revalidation. См. [governed semantic
editor](governed-semantic-editor.md).

## Контракт v1

Версионированный набор имеет схему `bhm.semantic-assurance.v1`. Он принимает
только content-free classifier output либо безопасный `abstain`; в receipt не
попадают prompt, memory text, доказательства, raw answer, credentials или
локальные пути.

Каждый case размечен отдельно по четырём осям:

- factuality: `true`, `false`, `insufficient` или `ambiguous`;
- entailment кандидата: `supported`, `contradicted` или `insufficient`;
- relevance каждой support-row: `relevant` или `irrelevant`;
- conflict: `none`, `direct`, `temporal`, `scope` или `implicit`.

Безопасный результат для недостаточных, неоднозначных, противоречивых или
conflicted оснований — `no_op`/`abstain`. Неподдержанный non-`no_op` lifecycle
suggestion считается unsafe false positive.

## Корпус и независимость

Public baseline v1 — синтетический, redacted и source-only набор для проверки
схемы, split isolation, evaluator и fail-closed поведения. Он не даёт права
называть метки независимой human evaluation только по факту публикации.

Для содержательного human-labeled evidence нужен отдельный принятый корпус с
проверяемыми metadata автора разметки, adjudication и dataset digest. Метки
создаются вне оцениваемой модели и не передаются ей во входе. Такому корпусу
нужны фиксированные partitions `development`, `validation` и
`sealed_holdout`; одна fact family, source document, prompt template и
paraphrase cluster не могут пересекать partitions. Sealed holdout не меняют по
результатам теста.

## Pre-admission private corpus до human review

До передачи private corpus reviewers запускается local-only pure verifier
`bhm.semantic-assurance-corpus-admission.v1`:
`scripts/validate-bhm-semantic-assurance-corpus.py --input <private-json>`.
Он читает один owner-controlled JSON, не делает сетевых вызовов, не открывает
SQLite/Mem0/Qdrant и печатает либо отказ, либо content-free receipt.

`bhm.semantic-assurance-corpus-admission.v1` сохраняется для historical/parser
compatibility: минимум `80` cases в `development`, `80` в `validation`, `400`
в `sealed_holdout`, не менее `800` sealed support-row и по `50` cases в каждом
direct/temporal/scope/implicit conflict bucket. Это минимум формы corpus, не
статистическое доказательство универсальной точности.

`bhm.semantic-assurance-corpus-admission.v2` — historical V6
human-curation contract с `admission_profile=v6_independent_human_curation`.
Он остаётся поддерживаемым для уже сформированной historical цепочки, но не
может быть повышен до curator-audited tier. Его hard-coded floor:

- `development >= 220`, `validation >= 220`, `sealed_holdout >= 400`;
- все 11 risk classes присутствуют в каждой из трёх partitions;
- каждый sealed conflict bucket содержит не менее `50` cases, всего не менее
  `200` sealed conflict-positive cases;
- private `v6_curation_controls` дают one-to-one declared binding
  `case_id -> atomic_fact_id`, neutral control cells и closed-vocabulary
  surfaces; controls не уходят в reviewer packets или public receipt.

V2 receipt несёт только content-free `partition_risk_counts` и control binding
digest. Он доказывает, что эти declared structural bounds были проверены, но
не доказывает человеческую идентичность, реальную независимость фактов,
происхождение источника, factual truth, entailment или relevance. V1 receipt
не может выдаваться за V6 receipt, а V2 — за source-audited V3 receipt.

Curator-audited V4 human-label admission остаётся historical compatibility
contract. Forward-only V5 human-label admission проверяет пригодность sealed
corpus к заранее объявленному readiness experiment: для каждого из десяти
mandatory exact-label strata требуется минимум `80` observations. Набор без
`false`/`ambiguous`/`insufficient`, `contradicted`, `irrelevant` или четырёх
conflict labels больше не может формально пройти V5, а затем неизбежно
провалиться в readiness. Эта проверка structural и content-free: она не
превращает labels в semantic truth и не аутентифицирует human process.

### V6 curator-source audit до построения corpus

До того как человек вручную создаёт reviewer-visible case, private ledger
`bhm.semantic-assurance-human-curation-ledger.v1` и его paired controls
`bhm.semantic-assurance-human-curation-controls.v1` проходят отдельный
локальный audit:

```powershell
uv run python scripts/validate-bhm-semantic-assurance-curator.py `
  --atomic-facts E:\BHM-private-evidence\semantic-assurance-v2\full-v6-curation-20261018\atomic-facts.json `
  --control-cells E:\BHM-private-evidence\semantic-assurance-v2\full-v6-curation-20261018\control-cells.json `
  --canonical-root E:\GitHub\repos\BlackHoleMemory `
  --output E:\BHM-private-evidence\semantic-assurance-v2\full-v6-curation-20261018\curator-audit-receipt.json
```

Он принимает только явно переданные local files, canonical Markdown из
`docs/**/*.md` и local canonical ADR из `.docs/adr/**/*.md`. Path traversal,
symlink, runtime/config/token/private paths, non-Markdown source, missing exact
heading, out-of-range lines или fragment hash mismatch отклоняются до receipt.
Fragment SHA-256 считается по указанным inclusive строкам после UTF-8/LF
normalization и без terminal newline. `docs/` фрагмент остаётся public-source
evidence; `.docs/adr/` intentionally local-only, поэтому receipt связывает его
content digest, но не выдаёт за Git-revision evidence.

Каждый private atomic fact обязан быть unique opaque SHA-256 ID, иметь
human-verified canonical fragment, unique normalized private proposition и
inference mechanism. Paired neutral control cell связывает его ровно с одним
risk class и closed-vocabulary surface profile. Audit требует минимум
`220/220/400`, все 11 risks в каждой partition и sealed direct/temporal/scope/
implicit buckets по `>=50`.

Для передачи кураторам без доступа к checkout используется отдельный
non-overwriting workpack builder. Он делает snapshot только разрешённых
Markdown sources, копирует portable read-only validator и создаёт **пустые**
working ledgers — он не выбирает fragments, не создаёт rows, cases или labels:

```powershell
uv run python scripts/prepare-bhm-semantic-assurance-curator-workpack.py `
  --canonical-root E:\GitHub\repos\BlackHoleMemory `
  --output E:\BHM-private-evidence\semantic-assurance-v2\V6-CURATOR-RECOVERY-<date>
```

Builder требует clean Git checkout и записывает `git_head` плюс digest snapshot
в `SOURCE-SNAPSHOT-MANIFEST.json`. Сначала куратор вручную создаёт несколько
реальных source bindings и запускает `RUN-ROW-CHECKPOINT.cmd`: этот checkpoint
проверяет только уже введённые bindings и ожидаемо сообщает
`coverage_complete=false`. Полный `RUN-FULL-AUDIT.cmd` допустим лишь после
реального набора floor. Перед дальнейшей admission цепочкой owner повторяет
audit against current clean canonical root at that exact `git_head`: snapshot
удобен для authoring, но не заменяет final source revalidation.

Receipt содержит только counts и three content-free bindings: atomic-fact,
control и canonical-fragment. В нём нет proposition, mechanism, source text,
path, heading, line range, identity, prompt, candidate, support или labels.
Он не создаёт corpus/packets, не аутентифицирует куратора, не доказывает
происхождение source beyond re-read bytes, semantic independence, factual
truth, entailment или relevance. Следующий допустимый шаг остаётся ручным:
один admitted atomic fact → максимум один independently authored unlabeled
case. Автоматическое построение case из private proposition запрещено.

Historical V6 corpus receipt V2 может перейти в dual-review только через
`bhm.semantic-assurance-human-label-admission.v3`: этот forward-only contract
принимает только corpus receipt V2, заново сверяет его exact dataset binding,
`220/220/400` distribution, partition-level risk map и control binding digest.
Его content-free label receipt сохраняет эти bindings, но всё ещё не
аутентифицирует reviewers или adjudicator. Новый curator-audited path использует
только corpus receipt V3 и label receipt V4, как описано в разделе
«V6 forward-only propagation». Existing label-admission v1/v2 сохраняют
исходный смысл и не могут молча повыситься до V6. Ни один corpus receipt не
даёт разрешения model run, automatic approve или automatic apply.

Verifier проверяет opaque non-risk IDs, одинаковый payload set с разными
позициями в A/B packets, blank ledger в порядке каждого packet, отсутствие
visible source/anchor locators, exact packet-to-corpus binding,
NFKC/casefold/whitespace text-collision между partitions и declared isolation
для family/group/source/theme/template/paraphrase identities. Reviewer видит
только `case_id`, `prompt`, `candidate` и `supports` в форме
`{support_id, evidence}`; partition, risk и curator metadata исключены.

Curator ledger содержит уникальные `semantic_id`, `proposition_id`,
`mechanism_id` и только SHA-256 identities source fragments. Входное claimed
attestation связывает opaque `curator_identity_sha256` с digest exact private
corpus + curator ledger. Это не аутентифицирует личность, не доказывает
происхождение фрагментов, их semantic independence, factual truth или
relevance. Receipt остаётся content-free и не публикует identity или source.
Он содержит только digest этого binding, чтобы later-stage verifier мог
fail-closed проверить наличие curator-to-corpus attestation.

Последовательность: **corpus pre-admission → независимые A/B reviewers →
freeze ledger/hashes → identity-neutral adjudication → final-label admission →
freeze sealed commitment/pre-registration → read-only local-model run**.
Отказ на любом шаге запрещает distribution или следующий шаг; receipt не
разрешает lifecycle mutation.

## Admission dual human review

Для private corpus readiness-path требует локальный contract
`bhm.semantic-assurance-human-label-admission.v2`. Он принимает только exact
binding к уже загруженному dataset: два различных opaque reviewer digest,
полное покрытие одинакового набора case/support IDs, один отличный от них
adjudicator digest и final labels, точно совпадающие с dataset labels.

V2 принимает только отдельно проверенный corpus pre-admission receipt и
переносит его content-free binding: digest полного набора
`case_id/partition/risk_class/support_ids`, digest sealed case set, digest
sealed support associations и counts всех 11 risk classes. Label admission
пересчитывает эти bindings из final dataset и отвергает замену case, support,
risk class или partition даже при тех же aggregate cardinality. V1 остаётся
только structural/parser compatibility и не является evidence tier для
readiness.

Manifest и raw review ledgers должны оставаться в owner-controlled ignored
location; public Git и receipt содержат только digests, counts и declared
protocol assertions. Команда
`scripts/validate-bhm-semantic-assurance-human-labels.py` выдаёт content-free
receipt. Его можно передать offline runner через `--label-admission`; mismatch
dataset/labels/digest или неполный review блокируют evaluation до scoring.

Dual-label admission не заменяет corpus pre-admission: он не подтверждает, что
packets были blind до distribution или что split isolation/source-fragment
independence уже проверены. Public synthetic v1 fixture годится только для
parser/structural tests и никогда не повышает tier до human corpus или
readiness evidence.

Это проверяет structural binding заявленного dual-review процесса, но не
аутентифицирует личности reviewers, не доказывает, что blind process реально
состоялся, и не превращает synthetic v1 fixture в human evidence. Поэтому
receipt формулирует tier только как
`attested_structurally_bound_not_identity_authenticated`; даже при нём
`automatic_apply_ready=false` и `automatic_apply_decision=no_go`.
Admission намеренно отвергает corpus с `fixture_reference_contract` и требует
явные source/theme/template/paraphrase isolation identities: к synthetic v1
нельзя приложить новый manifest задним числом для повышения evidence tier.

До dual review private corpus должен зарегистрировать protocol ID, timestamp,
digest threshold policy и exact digest sealed holdout. Contract разрешает
tuning только на `development`, требует frozen `validation` и immutable
`sealed_holdout`. Он также требует весь adversarial taxonomy: factuality,
entailment, relevance, direct/temporal/scope/implicit conflict, injection,
cross-project isolation, retrieval pressure и abstention; все critical classes
обязательны и в sealed holdout. Изменение labels, risk class или split после
commitment меняет digest и fail-closed блокирует admission.

## Readiness receipt: механическая полнота не есть release

Локальный verifier `bhm.semantic-assurance-readiness.v2` читает только один
content-free JSON evidence package и возвращает content-free aggregate receipt.
Его CLI — `scripts/validate-bhm-semantic-assurance-readiness.py`. Он
fail-closed связывает exact verified human-label admission, exact evaluated
model/run + prompt/dataset identity, pre-registered threshold policy, metrics
и Wilson lower bounds, sample minima, риск-таксономию, нулевые unsafe и
missed-conflict outcomes, exact-label stratified gates, repeat agreement,
read-only shadow/human-oracle evidence, disposable SQLite fault/rollback
rehearsal, architecture invariants и отдельный owner charter binding.

Каждая вложенная schema и digest проверяются точно; неизвестное поле, weak
lower bound, неполный риск-класс, mismatch corpus/run/policy или side effect в
shadow/rehearsal блокируют receipt. Для sealed measurement нужны минимум `400`
cases, `800` support rows и ровно `3` repeat. Каждый repeat обязан покрыть
точный sealed case digest и иметь ровно `400` chat calls; aggregate обязан
содержать ровно `1200` calls. Reports и execution ledger привязаны к
repeat-number, result/prediction digest и exact frozen identity
`qwen/qwen3.5-9b@q6_k` на `http://127.0.0.1:13666`. Это ловит обычную
подмену, агрегатный forgery и копирование одного report в три repeat, но
самосогласованный JSON не доказывает физическое выполнение HTTP/LLM calls
против сознательно подделывающего evidence производителя.

В public result намеренно нет corpus, prompt, model answer, local path или
reviewer/operator identity.

Даже если все эти механические prerequisite выполнены, единственный успех
verifier — `status=evidence_complete_but_release_not_authorized` с
`automatic_apply_ready=false` и `decision=no_go`. Это агрегатор evidence, не
admission, не lifecycle mutation и не owner release authorisation. Отдельный
owner safety/deployment decision остаётся внешним обязательным gate.

## Метрики и gates

Receipt фиксирует dataset/model/variant/prompt digests, endpoint identity,
время, repeat agreement при `temperature=0`, partitions и агрегаты — без
содержимого case. Метрики публикуются раздельно, с Wilson confidence interval:

Live classifier требует ровно один JSON object. Все labels обязаны быть JSON
strings в двойных кавычках; bare label token, Python-style dictionary, code
fence и любой текст за пределами JSON отклоняются fail-closed. Формат задаёт
portable prompt contract, а server-side `response_format` не участвует в
execution contract: любой transport failure остаётся fail-closed. Это сохраняет
строгий parser contract: исправление format-following модели не подменяет
семантическую оценку и не допускает ослабления schema validation.

Чтобы модель не искажала opaque support IDs, prompt передаёт evidence только с
ephemeral references `support_1`, `support_2` и так далее. Ответ обязан
содержать ровно это множество references; parser детерминированно отображает
их обратно в canonical support IDs до evaluation. Missing, extra или renamed
reference fail-closed отклоняется — это не эвристика и не снижение покрытия.

Live adapter допускает не более `512` cases в одной выбранной partition. Этот
предел остаётся транспортной защитой, но допускает sealed-holdout минимум из
`400` cases для readiness policy; он не ослабляет exact-model, loopback-only,
read-only или `NO-GO` ограничения.

- factuality accuracy;
- entailment precision/recall;
- relevance precision/recall по support rows;
- conflict recall и unsafe false positives;
- abstention для insufficient/ambiguous evidence;
- cross-project и authority-boundary violations.

Нельзя заменять raw model metrics effective-policy metrics или сливать четыре
оси в один score. Для lifecycle-changing false positive действует нулевой
наблюдаемый budget: любой такой пример закрывает gate. Наблюдаемые 100% conflict
recall и безопасные abstentions всё равно сопровождаются интервалами, а не
трактуются как гарантия.

Минимальные aggregate gates readiness: Wilson lower-95 не ниже `0.95` для
factuality, entailment, support relevance и conflict recall; `0` unsafe
non-`no_op` и `0` missed conflict. Эти aggregate gates необходимы, но сами по
себе не доказывают качество по редкому label или отдельному conflict bucket.

Readiness v2 дополняет их обязательными exact-label strata. Для каждого stratum
readiness заново выводит population и результат из canonical `results`, а не
принимает caller-supplied aggregate. Для factuality, entailment и conflict
population — cases, у которых ожидаемая метка равна stratum; success — только
та же exact prediction. Для support relevance population — отдельные
support-row с ожидаемой меткой, а success — exact prediction этой строки.
Например, `direct → temporal` не засчитывается ни как успех `direct`, ни как
успех `temporal`, даже если такая ошибка не является `missed_conflict`.

Ровно следующие десять strata обязательны в sealed measurement:

- factuality: `false`, `ambiguous`, `insufficient`;
- entailment: `contradicted`, `insufficient`;
- support relevance: `irrelevant`;
- conflict: `direct`, `temporal`, `scope`, `implicit`.

Для каждого из них policy требует минимум `80` наблюдений и Wilson lower-95
для exact-label recall не ниже `0.95`. Недостающий, лишний, переименованный
или слабее заданный stratum fail-closed отклоняет readiness evidence. Эти
границы повышают строгость ограниченного измерения, но остаются конечной
выборкой: они не доказывают universal factual truth, entailment, relevance
или отсутствие hidden conflicts вне corpus.

Прохождение evaluator подтверждает только его объявленный bounded scope. Любое
обсуждение будущего automatic apply требует отдельного safety/deployment
charter, независимого review corpus, воспроизводимых holdout results и
отдельного owner decision; этот контракт сам по себе такого решения не даёт.

## Trusted-runner readiness v3

Readiness v3 добавляет integrity-контур поверх v2, не расширяя его смысл и не
разрешая lifecycle mutation. Вход имеет схему
`bhm.semantic-assurance-readiness.v3` и связывает source identity, live receipt
v3, event chain, v2 evidence, policy и отдельный trusted-runner attestation.

Порядок вычисления намеренно не образует hash-цикл:

1. verifier повторно проверяет v2 evidence и строит content-free
   `pre-attestation core`;
2. digest этого core (`pre_attestation_readiness_digest`) связывается с
   подписью runner;
3. только после проверки подписи строится финальный v3 receipt.

Live v3 receipt содержит ровно 1200 content-free событий для sealed run:
   `sequence`, `repeat_number`, `case_id`, `request_digest`,
   `response_digest`, `event_digest`. Ordered hash-chain связывает каждое
событие с предыдущим digest и inventory before/after. Verifier дополнительно
требует три повтора по одному набору из 400 case IDs; missing, duplicate,
reordered или substituted events fail closed. Prompt, evidence и raw model
output в receipt не попадают.

Trusted-runner attestation использует purpose-limited Ed25519 registry и exact
bindings к source revision/tree, runner manifest, frozen model/prompt, corpus,
labels, policy, live report, pre-attestation digest, event-chain anchors и
inventory digests. Пустой checked-in registry — намеренный fail-closed default:
до owner-issued ключа ни одна production-like attestation не может пройти.

Подпись доказывает только то, что доверенный ключ подписал данный bounded
content-free envelope. Она не доказывает физическое выполнение HTTP-вызовов на
скомпрометированном хосте, соответствие весов модели заявленному variant,
универсальную factual truth, полную hidden-conflict detection или open-world
safety. Даже при успешной механической проверке v3 receipt всегда содержит
`automatic_apply_ready=false` и `decision=no_go`.

## V6 forward-only propagation: curator audit, evaluator, live v5, privacy split и readiness v6

Для source-audited V6 действует отдельная forward-only цепочка:

`curator-audit receipt.v1 → corpus-admission receipt.v3 →`
`human-label-admission receipt.v5 → evaluator report → live receipt.v5 →`
`readiness receipt.v5 (pre-attestation core.v3)`.

Историческая цепочка `corpus-admission receipt.v2 → label receipt.v3 → live
receipt.v4 → readiness receipt.v4` остаётся валидной в своей исходной границе,
но не может быть переименована, дополнена или молча повышена до
source-audited V6. То же относится к старым evaluator reports, live v1/v2/v3
и readiness v2/v3: они сохраняют прежний смысл и не являются доказательством
нового tier.

### Curator-audit receipt.v1 и corpus receipt.v3

`bhm.semantic-assurance-curator-audit-receipt.v1` остаётся content-free. Он
связывает три независимых digest-а: `atomic_fact_binding_digest`,
`control_binding_digest` и `canonical_fragment_binding_digest`, а также их
aggregate counts. Сам digest receipt недостаточен для join: corpus V3 обязан
повторно вычислить и сопоставить все три binding-а с private
`curator_case_bindings`.

Каждая строка `curator_case_bindings` имеет ровно один opaque `case_id` и
`atomic_fact_id`, exact canonical fragment SHA-256 и `source_kind`, а также
closed-vocabulary control profile: support-count, length bucket, document
genre, relation/adversarial form и surfaced dimensions. Corpus V3 требует
one-to-one case/control coverage, exact partition/risk recomputation и
`admission_profile=v6_curator_audited_independent_human_curation`. Изменение
одного case, atomic fact, control, fragment hash или source kind отклоняется,
даже если aggregate counts совпадают.

Receipt V3 сохраняет structural floors (`development >=220`,
`validation >=220`, `sealed_holdout >=400`), all-11-risk coverage и sealed
conflict floors. Он доказывает только корректность declared join и повторной
проверки канонических bytes; он не доказывает curator identity, human
independence, factual truth, entailment, relevance или полноту hidden
conflicts. Автоматическое построение reviewer cases из private propositions
запрещено.

### Label V4/V5, evaluator binding и live V5

`bhm.semantic-assurance-human-label-admission.v4` принимает только exact
corpus receipt V3 и повторяет его curator binding byte-for-byte. V1/V2/V3
label contracts не меняются и остаются historical/compatibility paths. V4
сохраняет своё исходное значение и не утверждает readiness-eligible coverage
exact labels.

Forward-only `bhm.semantic-assurance-human-label-admission.v5` дополнительно
пересчитывает только из final blind-adjudicated labels sealed populations
десяти readiness strata: factuality `false`/`ambiguous`/`insufficient`,
entailment `contradicted`/`insufficient`, support relevance `irrelevant` и
conflict `direct`/`temporal`/`scope`/`implicit`. Первые, вторые и conflict
strata считаются по cases; relevance — по support rows. Каждый требует не
менее `80` observations. V5 receipt сохраняет только fixed floor, boolean и
SHA-256 canonical count-map; raw labels, counts, case IDs, supports, prompts
и evidence не публикуются. Недобор любого stratum fail-closed отклоняет V5.
Это исключает structurally V5-admitted, но математически readiness-impossible
corpus; V4 receipts остаются допустимы лишь в своей historical границе.

`bhm.semantic-assurance-human-label-admission.v6` — отдельное, forward-only
усиление V5. Оно сохраняет все десять V5 exact-label floors и дополнительно
коммитит content-free SHA-256 matrix `11 risk classes × 10 exact-label strata`.
Каждый stratum должен быть представлен хотя бы в двух sealed risk classes; для
`direct_conflict × direct`, `temporal_conflict × temporal`,
`scope_conflict × scope` и `implicit_conflict × implicit` требуется не менее
50 observations в aligned cell. Relevance по-прежнему считается по support
rows, наследуя risk class от owning case. Admission, evaluator, live adapter и
V7 readiness заново выводят matrix из exact owner-only labels; digest сам по
себе не принимается на доверии. V1–V5 остаются historical contracts и не
получают эти свойства задним числом.

Эта matrix закрывает distribution loophole: глобальные `>=80` по label не
могут подменять coverage соответствующего conflict risk. Но floor `>=2` risk
classes — только structural diversity. Он не даёт отдельный 95% Wilson claim
для каждой risk×label ячейки: для этого нужен будущий явно объявленный
per-cell policy с существенно большей выборкой.

До повторной human-curation owner может получить content-free gap diagnosis,
не меняя dataset, manifest или labels:

```powershell
uv run python scripts\diagnose-bhm-semantic-assurance-v5-strata.py `
  --dataset <owner-only-final-dataset.json> `
  --manifest <owner-only-label-admission-manifest.v4.json> `
  --output <owner-only-v5-strata-diagnostic.json>
```

Результат имеет схему
`bhm.semantic-assurance-v5-readiness-strata-diagnostic.v1`: для каждого из
десяти закрытых stratum он печатает только aggregate `population` и `deficit`
от fixed floor `80`, плюс exact dataset/manifest/corpus bindings. В нём нет
case IDs, support IDs, reviewer/adjudicator identities, prompts, source text
или raw labels. Код возврата `2` означает `readiness_eligible=false`; это
диагностика composition gap, не V5 admission и не разрешение переписывать
метки или выполнять lifecycle mutation. Даже `readiness_eligible=true`
покрывает только один prerequisite V5 и неизменно сохраняет
`automatic_apply_ready=false`, `automatic_apply_decision=no_go`.

До scoring evaluator повторно проверяет receipt V3, exact dataset binding,
`partition_risk_counts`, `v6_control_binding_digest`, `curator_audit` и link к
`corpus_pre_admission_digest`. Эти content-free values входят в dataset
раздел evaluator report. Отчёт с label receipt V3/V4 mismatch не является
V6 evidence.

Live adapter выпускает
`bhm.semantic-assurance-live-receipt.v5` только для strata-bound label receipt V5,
а `bhm.semantic-assurance-live-receipt.v6` — только для matrix-bound label
receipt V6; проверка выполняется до создания HTTP client. Receipt переносит
exact curator binding без prompt, evidence или raw model output.
Это read-only transport evidence, а не доказательство фактического запуска
модели или качества ответа.

### Privacy-split live V6, historical readiness V6 и readiness V7

Legacy live receipts V1–V5 остаются restricted owner artifacts: в них есть
`offline_reports` с per-case evaluation rows. Они не должны становиться
public/checked-in receipt. Forward-only helper строит из уже проверенного V5
или V6 receipt два exact-bound файла без hash-cycle:

```text
public-live-receipt.v6              # content-free: aggregates, identities and commitments only
owner-only-private-evaluation.v6    # exact offline_reports; required for recomputation
```

Public receipt коммитит exact legacy receipt digest и ordered
`offline_reports_commitment`, но не содержит case IDs, predictions, labels,
support IDs или evidence. Private bundle коммитит digest уже построенного
public receipt и тот же reports commitment. V6 readiness требует оба: сначала
проверяет public/private cryptographic join, затем заново выводит metrics,
strata, safety counts, repeat coverage и case binding из owner-only reports.
Таким образом public aggregate нельзя подменить без private evidence, а private
report нельзя присоединить к другому public run. Финальный readiness receipt
снова остаётся content-free.

Historical `bhm.semantic-assurance-readiness.v6` принимает только label receipt
V5 и поэтому не заявляет risk×label matrix gate. Новый
`bhm.semantic-assurance-readiness.v7` принимает только label receipt V6 и
legacy live receipt V6, повторно выводит matrix из каждого owner-only report и
сверяет её с admission binding до проверки runner attestation. V7 receipt
выдаёт только content-free risk-stratified metrics и сохраняет
`automatic_apply_ready=false`, `decision=no_go`.

Для запуска с split-output используются оба явных пути; обычный `--output`
совместим, но пишет restricted legacy receipt:

```powershell
uv run python scripts\run-bhm-semantic-assurance-live.py `
  --dataset <owner-only-final-dataset.json> `
  --label-admission <label-admission-receipt.v6.json> `
  --partition sealed_holdout --repeats 3 `
  --public-output <public-live-receipt.v6.json> `
  --private-evaluation-output <owner-only-private-evaluation.v6.json>
```

Эта команда не загружает, не выгружает и не переключает модель: она сохраняет
только already-loaded exact model contract. Public и private V6 outputs не
являются разрешением на lifecycle mutation.

### Readiness V5 и trusted-runner boundary

Readiness V5 принимает только согласованные strata-bound label receipt V5, evaluator report
и live receipt V5. Он сверяет byte-for-byte curator bindings, включает их в
`pre-attestation core.v3`, а затем в финальный readiness receipt. Existing
runner attestation envelope не расширяется: подписывается digest core после
того, как core содержит exact curator binding, поэтому hash-cycle не
возникает.

Механическая continuity этой цепочки не доказывает identity или независимость
human reviewers, factual truth, entailment, relevance, полноту hidden
conflicts, фактическое HTTP/model execution либо универсальную безопасность.
Даже полностью сформированный V5 receipt неизменно возвращает
`automatic_apply_ready=false` и `decision=no_go`. Реальный V6 corpus,
dual-review/adjudication, sealed local-model run, shadow/human-oracle,
disposable SQLite fault/rollback rehearsal и отдельный owner
safety/deployment decision остаются обязательными gates. Automatic semantic
approve/apply не становится разрешённым ни на одной стадии этой цепочки.
