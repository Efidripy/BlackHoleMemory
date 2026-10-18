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

## Admission dual human review

Для private corpus добавлен локальный contract
`bhm.semantic-assurance-human-label-admission.v1`. Он принимает только exact
binding к уже загруженному dataset: два различных opaque reviewer digest,
полное покрытие одинакового набора case/support IDs, один отличный от них
adjudicator digest и final labels, точно совпадающие с dataset labels.

Manifest и raw review ledgers должны оставаться в owner-controlled ignored
location; public Git и receipt содержат только digests, counts и declared
protocol assertions. Команда
`scripts/validate-bhm-semantic-assurance-human-labels.py` выдаёт content-free
receipt. Его можно передать offline runner через `--label-admission`; mismatch
dataset/labels/digest или неполный review блокируют evaluation до scoring.

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

## Метрики и gates

Receipt фиксирует dataset/model/variant/prompt digests, endpoint identity,
время, repeat agreement при `temperature=0`, partitions и агрегаты — без
содержимого case. Метрики публикуются раздельно, с Wilson confidence interval:

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

Прохождение evaluator подтверждает только его объявленный bounded scope. Любое
обсуждение будущего automatic apply требует отдельного safety/deployment
charter, независимого review corpus, воспроизводимых holdout results и
отдельного owner decision; этот контракт сам по себе такого решения не даёт.
