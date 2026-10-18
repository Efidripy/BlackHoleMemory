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

Для V6 human-curation path нужен только отдельный forward-only contract
`bhm.semantic-assurance-corpus-admission.v2` с
`admission_profile=v6_independent_human_curation`. Его hard-coded floor:

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
не может выдаваться за V6 receipt.

V6 corpus receipt может перейти в dual-review только через
`bhm.semantic-assurance-human-label-admission.v3`: этот forward-only contract
принимает только corpus receipt v2, заново сверяет его exact dataset binding,
`220/220/400` distribution, partition-level risk map и control binding digest.
Его content-free label receipt сохраняет эти bindings, но всё ещё не
аутентифицирует reviewers или adjudicator. Existing label-admission v1/v2
сохраняют исходный смысл и не могут молча повыситься до V6. Отдельный readiness
v4 должен потреблять именно этот label receipt; до него V6 corpus не даёт
разрешения model run или automatic apply.

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

## V6 propagation: evaluator, live v4 и readiness v4

V6 evidence идёт только по отдельной forward-only цепочке:
`corpus-admission receipt.v2 → human-label-admission receipt.v3 →`
`evaluator report → live receipt.v4 → readiness receipt.v4`. Старые
evaluator reports, live v1/v2/v3 и readiness v2/v3 не меняют смысл и не могут
молча стать V6 evidence.

До scoring evaluator повторно сверяет corpus receipt.v2,
`admission_profile=v6_independent_human_curation`, exact
`partition_risk_counts`, `v6_control_binding_digest` и link к
`corpus_pre_admission_digest`. Эти content-free bindings попадают в dataset
раздел evaluator report. Live adapter повторяет проверку до создания HTTP
client и выпускает `bhm.semantic-assurance-live-receipt.v4` только для
label receipt.v3; он несёт те же exact bindings без prompt, evidence или raw
model output.

Readiness v4 принимает только пару label receipt.v3 и live receipt.v4,
сверяет их byte-for-byte нормализованные V6 bindings, включает их в
`pre-attestation core.v2` и финальный receipt. Подпись runner остаётся
attestation v2 и подписывает только pre-attestation digest, поэтому hash-цикл
не возникает. V6 floors являются минимумами (`development >=220`,
`validation >=220`, `sealed_holdout >=400`), а не требованием ровно 840 cases.

Это сохраняет declared structural continuity, но не доказывает identity или
независимость humans, factual truth, entailment, relevance, полноту hidden
conflicts, фактическое HTTP/model execution либо универсальную безопасность.
Даже сформированный v4 receipt неизменно возвращает
`automatic_apply_ready=false` и `decision=no_go`; реальный V6 corpus,
dual-review/adjudication, sealed model run и owner safety/deployment decision
остаются отдельными обязательными gates.
