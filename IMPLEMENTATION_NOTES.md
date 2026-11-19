# Implementation notes — 2026-11-19

## Kết quả và phạm vi đã kiểm chứng

- Branch: `feat/laya-td-pilot-v0`. Workspace chưa có repository; tạo repository trong `slm_mas_pilot/`. `main` giữ baseline đặc tả ở commit `ff80eb0`; feature branch chứa triển khai. Chưa cấu hình remote, chưa push.
- Python 3.11.9; source/mock/CLI, data preparation, reporting và các kiểm thử invariant đã chạy.
- **36 tests pass, 6 tests Docker skip**. Không có Docker Linux/image trên máy để thực thi sáu integration case. Skip không được tính là sandbox đã kiểm chứng.
- Wheel project build/install thành công bằng `pip install .`. CLI dùng `PYTHONPATH=src` để kiểm tra checkout đang sửa.
- Checkpoint tokenizer thật + renderer của SDK Laya 0.4.37 đã chạy: head 46 token với 2 action, 72 token với 4 action; không cắt options. Kiểm tra long-code view vẫn vừa 1024 token; phát hiện SDK state/option truncation. Đây **không phải inference Laya**.
- MBPP+ v0.2.0: 378 task thực tế, sample 20 ID seed 42, 20 public exports hợp lệ, không thay ID. Hidden/reference data không được in ra model payload hoặc chạy trên host.
- Mock smoke: ba task synthetic, mỗi task 2 worker calls, 3 router decisions; final public pass, final benchmark evaluation `not_run`.
- Real smoke 3 task và pilot 20 task: **blocked**, không có Laya/Qwen generation hoặc EvalPlus score thật. Mỗi selected ID có episode blocked, không bị bỏ khỏi report.

## Bằng chứng trên đĩa

| Nội dung | Đường dẫn |
|---|---|
| Mock run hoàn chỉnh | [runs/20261119T154728-mock-fdd99aa8/report.md](runs/20261119T154728-mock-fdd99aa8/report.md) |
| Mock episode inspect | [episode_result.json](runs/20261119T154728-mock-fdd99aa8/episodes/synthetic_add-0-d021296a/episode_result.json) |
| Real smoke blocked, đúng ba ID đầu | [runs/20261119T154729-real-cbc0bab0/report.md](runs/20261119T154729-real-cbc0bab0/report.md) |
| Pilot 20 ID blocked | [runs/20261119T154730-real-faa6ad04/report.md](runs/20261119T154730-real-faa6ad04/report.md) |
| Doctor hiện tại | [verification/doctor.json](verification/doctor.json) |
| Token renderer thật, chưa inference | [verification/laya_renderer.json](verification/laya_renderer.json) |
| Data audit | [verification/preparation_audit.json](verification/preparation_audit.json) |
| Manifest + exposure đã cập nhật | [verification/tasks.manifest.json](verification/tasks.manifest.json) |
| Checkpoints / chat template | [verification/checkpoints.json](verification/checkpoints.json) |
| Test result / SDK wheel hashes | [tests.json](verification/tests.json), [sdk_wheels.json](verification/sdk_wheels.json) |

`runs/`, `.research/`, `.venv/`, dữ liệu raw và private được gitignore. Chúng tồn tại tại workspace hiện tại; `verification/` giữ metadata nhỏ để review cùng source. Các run trước vẫn giữ lại, không overwrite hoặc gộp vào run cuối.

## Versions và immutable identities

| Thành phần | Giá trị / mức kiểm chứng |
|---|---|
| laya | 0.4.37; wheel thật đã tải/cài, import và renderer chạy được; chưa load weights |
| EvalPlus | 0.3.1; wheel thật đã tải/cài `--no-deps` để kiểm tra source, API và output schema; evaluator thật chưa chạy. Dockerfile cài dependencies trong image riêng. |
| Transformers | 5.9.0; tokenizer Laya chạy được từ snapshot local |
| PyYAML / pytest | 6.0.3 / 8.3.4 |
| Torch / NumPy / huggingface-hub | 2.12.0 / 2.4.4 / 1.16.4 trong môi trường kiểm tra |
| Laya-TD revision | `989e28febe5886389496fe13a8036c62b2a20ae0` |
| Qwen revision | `c03e6d358207e414f1eca0bb1891e29f1db0e242` |
| Qwen chat-template SHA-256 | `cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f` |
| Sanitized MBPP source commit | `f82046ba5aabbbb427dbfd38a254d26bff08b533` |
| Sanitized MBPP bytes SHA-256 | `ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9` |
| MBPP+ v0.2.0 bytes SHA-256 | `b54e762755248ca411b523c917fa9f93c07b5ff2966bf60b3917b853926a3dad` |

Metadata version installed không chứng minh mọi dependency đã sẵn sàng hoặc GPU inference tương thích. Virtualenv kiểm tra dùng `--system-site-packages` để tận dụng thư viện máy có sẵn; đây không phải environment lock portable. README hướng dẫn virtualenv riêng. Snapshot tokenizer chỉ tải các JSON nhỏ; không tải weights Laya/Qwen.

## Commands thực sự đã chạy

Chạy từ repository root. Trong kiểm tra source đặt `$env:PYTHONPATH='src'`; `.venv\Scripts\python.exe` là Python trong virtualenv.

| Command / thao tác | Kết quả |
|---|---|
| `git init -b feat/laya-td-pilot-v0 slm_mas_pilot` (từ workspace cha) | Repository mới, giữ nguyên tài liệu gốc/PDF |
| `python -m pip download --no-deps --dest .research laya==0.4.37 evalplus==0.3.1` | Tải wheel thành công, có SHA-256 lưu lại |
| `python -m venv --system-site-packages .venv` | Tạo môi trường kiểm tra local |
| `.venv\Scripts\python.exe -m pip install --no-deps <hai wheel local>` | Cài hai package để đối chiếu source; chưa cài mọi dependency EvalPlus |
| `pip install -e .` / `--no-build-isolation` | Không thành công: thiếu wheel ở env gốc, sau đó lỗi `.pth` encoding cp1252 với đường dẫn tiếng Việt của setuptools. Không đổi đường dẫn người dùng. |
| `.venv\Scripts\python.exe -m pip install .` | Wheel chuẩn build/install thành công |
| `python -m slm_mas.cli prepare-data --config configs/pilot_laya_mbpp.yaml` | Download snapshots thật, 378 task, seed 42/20 IDs |
| `prepare_from_files(...)` với cùng raw snapshots và seed | Tạo lại export sau khi sửa mapping citation URL; snapshot cũ được chuyển nguyên vẹn sang `data-pre-mapping-normalization/` |
| HTTP GET metadata/checkpoint tokenizer trên Hugging Face | Resolve revision, tải tokenizer Laya và chat-template config Qwen; không tải weights |
| `python scripts/check_laya_renderer.py --tokenizer-dir .research/laya-typed-decisions/tokenizer` | SDK/tokenizer integration pass, including option/state truncation and long-code context |
| `python -m pytest -q --basetemp=.test-tmp/acceptance --tb=short` | 36 passed, 6 skipped |
| `python -m compileall -q src scripts` | Pass |
| `python -m slm_mas.cli smoke --mock` | Ba episode hoàn chỉnh, report đầy đủ |
| `python -m slm_mas.cli doctor --config configs/pilot_laya_mbpp.yaml` | Báo các blocker hạ tầng đúng; checkpoint fields đã được bổ sung sau lần doctor đầu, static checks cuối lưu ở `verification/doctor.json` |
| `python -m slm_mas.cli smoke --config configs/pilot_laya_mbpp.yaml --limit 3` | Blocked, 3/3 task vẫn có outcome vận hành |
| `python -m slm_mas.cli run --config configs/pilot_laya_mbpp.yaml` | Blocked, 20/20 task vẫn có outcome vận hành |
| `python -m slm_mas.cli inspect --run-dir runs/20261119T154728-mock-fdd99aa8 --task-id synthetic/add-0` | Đọc episode không gọi model |
| `python -m slm_mas.cli report --run-dir runs/20261119T154728-mock-fdd99aa8` | Tái tạo report không gọi model/sửa online state |
| `git diff --check` | Pass; kiểm tra lại staged diff trước commit |

Trong các vòng kiểm tra đã sửa: lỗi TEMP/atomic rename của sandbox Windows bằng test directory trong checkout; eval temporary output nằm dưới `evaluation/` và chỉ chmod trên POSIX; dùng hash raw bytes cho dataset để không bị CRLF normalization; không tính system-error/no-code thành outcome xác định; snapshot router source-state phải khớp hash chính xác; worker usage thiếu vẫn null khi attempt cuối hỏng.

## Mapping và development exposure

Hai task Mbpp/749 và Mbpp/771 chứa URL tham khảo ở sanitized MBPP, trong khi MBPP+ đã bỏ URL. Đã kiểm tra statement, assertions và entry point công khai để sửa comparison: chỉ loại URL khi so sánh, giữ nguyên original text trong export. Không đọc hoặc dùng reference solution để tạo expected output. Không đổi sample hoặc sửa assertions.

Hai ID này được ghi trong `data/development_exposed_ids.json` và manifest verification/run mới vì đã được dùng chỉnh logic chuẩn bị dữ liệu. Baseline audit ban đầu có hai mapping error được giữ riêng; audit cuối có zero mapping errors. Ba smoke IDs là Mbpp/749, Mbpp/104, Mbpp/18.

## Blockers và phần cần integration trước khi diễn giải kết quả thật

1. Không có Docker executable/daemon/image. Chưa chạy container fixtures, kiểm chứng candidate timeout trên Linux hoặc offline EvalPlus. Không fallback `exec` trên host.
2. Không có `WORKER_BASE_URL`, served-model mapping và deployment attestation. Chưa có generation Qwen thật, usage/provider latency hay effective quantization/seed. API key chỉ cần nếu endpoint yêu cầu.
3. Model weights Laya chưa được tải/load. Tokenizer + renderer pass không chứng minh model inference/choice schema đúng trên checkpoint này. Preflight sẽ lưu real-response fixture trước real smoke khi setup đầy đủ.
4. Offline evaluator image chưa được build/pin. Không có `base_pass`/`plus_pass` thật; các evaluator integration trong unit tests dùng fake container response và được ghi rõ trong test.
5. Container/harness phù hợp pilot benign; chưa có chứng minh chống Python introspection hoặc host kernel escape. Không dùng kết quả để tuyên bố bảo mật trước đối thủ.

Các lựa chọn triển khai, timeout/resource limits, parser, token truncation, data boundary, sampling, logging và statistical denominators được giải thích trong [README.md](README.md). Chưa có evidence để kết luận Laya tốt hơn router khác, dynamic routing tốt hơn workflow cố định hoặc MAS tốt hơn single-agent.
