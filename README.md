# SLM MAS Pilot v0 — Laya-TD / Qwen / MBPP+

Triển khai đặc tả [SLM_MAS_PILOT_V0_LAYA_TD.md](docs/SLM_MAS_PILOT_V0_LAYA_TD.md): router chọn một action, worker thực hiện một generation, runtime lưu artifact mới và feedback public, rồi quay lại router. Final evaluation chạy bằng CLI riêng sau khi đóng băng candidate.

**Trạng thái:** source và mock end-to-end đã chạy; dữ liệu MBPP+ v0.2.0 đã được kiểm tra 378 task, manifest 20 task cố định đã chuẩn bị. Chưa có kết quả inference Laya/Qwen thật hoặc kết quả EvalPlus thật. Docker, endpoint Qwen và xác nhận deployment vẫn là blocker. Xem bằng chứng, commands và giới hạn kiểm chứng trong [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md).

Repository được tạo trong thư mục con vì workspace ban đầu chưa có Git/source. PDF và đặc tả gốc bên ngoài repository không bị sửa. Branch triển khai: `feat/laya-td-pilot-v0`.

## Chạy mock và kiểm thử

Python 3.11. Từ thư mục chứa README, trên PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install '.[test]'
$env:PYTHONPATH = 'src'
python -m pytest -q
python -m slm_mas.cli smoke --mock
```

`PYTHONPATH=src` giúp CLI dùng chính source đang sửa trong checkout. Trên Linux dùng `source .venv/bin/activate` và `export PYTHONPATH=src`. Base install chỉ cần PyYAML; mock không import Laya, Transformers hoặc EvalPlus và không gọi mạng. Mock executor nhận diện các fixture đã biết, **không thực thi Python do model tạo trên host**. Một mock smoke có ba task synthetic, mỗi task đi qua implementer → public fail → repairer → public pass → stop. `base_pass` và `plus_pass` vẫn null; public pass không biến thành điểm benchmark.

Windows trong môi trường Codex hiện tại chặn atomic rename ở thư mục TEMP mặc định. Có thể chạy tests với thư mục tạm bên trong checkout:

```powershell
New-Item -ItemType Directory -Force .test-tmp | Out-Null
python -m pytest -q --basetemp=.test-tmp/my-new-test-run
```

Dùng tên thư mục mới cho mỗi lần kiểm tra. Editable installation bằng setuptools hiện có lỗi encoding với đường dẫn tiếng Việt trên máy này; `pip install .` hoặc `PYTHONPATH=src` tránh đường `.pth` lỗi đó. Không cần đổi tên thư mục của người dùng.

## Chuẩn bị và chạy thật

```powershell
python -m pip install '.[router,worker]'
python -m slm_mas.cli prepare-data --config configs/pilot_laya_mbpp.yaml
python -m slm_mas.cli doctor --config configs/pilot_laya_mbpp.yaml
```

Prepare-data là thao tác có mạng: resolve commit của sanitized MBPP công khai, tải MBPP+ release v0.2.0, lưu snapshot và SHA-256. Không thực thi reference solution. Lệnh không ghi đè manifest đã tồn tại; dùng `--output data/new-version` và config mới khi đổi policy. Dữ liệu đã chuẩn bị trong workspace này, không cần tải lại.

Build hai image khi có Docker Linux:

```powershell
docker build -f docker/Dockerfile.public -t slm-public:v0 .
docker image inspect slm-public:v0 --format '{{.Id}}'
docker build -f docker/Dockerfile.evaluation -t slm-evaluation:v0 .
docker image inspect slm-evaluation:v0 --format '{{.Id}}'
```

Ghi image ID `sha256:...` thực tế vào `execution.image_digest` / `evaluation.image_digest`. Nếu cần rebuild tái lập, truyền `--build-arg PYTHON_BASE=python@sha256:<digest đã resolve>`. Tag trong Dockerfile chỉ là đầu vào bootstrap, không được dùng làm identity của run. Docker runtime dùng image pin đã có ở máy, không tự kéo image thay thế.

Cấu hình endpoint Qwen tự host hoặc provider tương thích chat-completions:

```powershell
$env:WORKER_BASE_URL = 'http://localhost:8000/v1'
$env:WORKER_API_KEY = '<key nếu endpoint yêu cầu>'
```

Không commit key và không điền key vào YAML. `.env.example` chỉ là mẫu, ứng dụng không tự load `.env`. Cần điền `workers.served_model` theo `/models`; xác nhận endpoint thực sự serve đúng checkpoint Qwen đã chọn, cùng tokenizer/chat template, context 8192, decoding và không có retry nội bộ. Điền `effective_decoding` bằng cấu hình đã xác minh, `quantization` nếu biết, rồi đặt `deployment_attested: true`. Đây là xác nhận cấu hình hạ tầng của người chạy, không phải chứng nhận từ API: một endpoint có thể công bố model ID mà không chứng minh weights của nó. Token usage khác local template sẽ được log rồi chặn episode.

Hai model revision và hash chat template trong config đã resolve từ nguồn chính thức; không đổi chúng chỉ để vượt doctor. Checkpoint Laya được tải bởi SDK khi preflight inference thực sự chạy. Cần có model cache/network cho lần load đầu. Worker weights nằm ở endpoint, ứng dụng chỉ tải tokenizer Qwen. Pin package trực tiếp trong `pyproject.toml`; run manifest ghi phiên bản transitive thực sự đã dùng. Chưa có lockfile transitive được chứng minh trên GPU.

```powershell
python -m slm_mas.cli doctor --config configs/pilot_laya_mbpp.yaml
python -m slm_mas.cli smoke --config configs/pilot_laya_mbpp.yaml --limit 3
python -m slm_mas.cli run --config configs/pilot_laya_mbpp.yaml
python -m slm_mas.cli evaluate --run-dir runs/<run_id>
python -m slm_mas.cli report --run-dir runs/<run_id>
python -m slm_mas.cli inspect --run-dir runs/<run_id> --task-id Mbpp/749
```

Doctor kiểm tra dependency, commit pin, endpoint model, tokenizer/template, Docker Linux và một public fixture thật; load Laya một lần, thử choice request ngắn và lưu request/response thật. `run` lặp preflight để trạng thái doctor cũ không che hạ tầng đã thay đổi; trong một run, cùng Laya process resident được dùng xuyên các episode. Real smoke dùng ba ID đầu của manifest; không sample lại. Nếu setup bị chặn, run lưu episode blocked cho tất cả ID đã chọn. Nếu chưa có manifest thì không bịa 20 ID: selected=0 và run-level blocker.

Evaluate đọc resolved config đã lưu trong run; có thể truyền `--config` để bổ sung image evaluator sau khi online run kết thúc. Candidate hash và dataset hash vẫn phải khớp; thay evaluator không được sửa online state. Kết quả chấm và effective image/limits nằm riêng trong `evaluation/`. Không overwrite evaluation đã có, không resume workflow đã nhìn final tests.

## Kiến trúc và các lựa chọn triển khai

Các lựa chọn dưới đây là engineering defaults để pilot dễ kiểm tra; chưa được chứng minh tối ưu về chất lượng model.

| Lựa chọn | Lý do và hệ quả |
|---|---|
| Python 3.11, dataclass + Protocol, vòng lặp nhỏ | Phù hợp hệ sinh thái Laya/EvalPlus, ít dependency cho mock. Dataclass giữ contract rõ; kiểm tra tại các boundary thay vì thêm framework điều phối. Chưa cần AutoGen/LangGraph, database hoặc distributed scheduling. |
| Ba role, cùng Qwen checkpoint và decoding | Giữ năng lực backbone cố định để quan sát lựa chọn role. Khác biệt prompt chưa chứng minh chuyên môn thật. Analyst không chỉ đạo action tiếp theo. |
| Tuần tự, concurrency=1, một generation/dispatch | Mỗi thay đổi artifact có một producer, dễ dựng lại handoff; không có vòng solve–test–repair ẩn trong worker. |
| Chỉ điều kiện hợp lệ ảnh hưởng action space | Chưa có code thì chỉ analyst/implementer. Có code thì repairer/stop hợp lệ dù public fail. Không có rule pass→stop hay fail→repairer làm thay router. |
| Giữ candidate mới nhất, không rollback | Quan sát được regression. Syntax-invalid fenced code vẫn là candidate; lỗi parser giữ candidate cũ. Không dùng final tests chọn bản tốt nhất. |
| Artifact hash SHA-256 + ID theo step | Hash phát hiện code không đổi; ID theo step phân biệt hai generation trả cùng code. Feedback gắn artifact ID; analysis ghi `based_on_code_id` để không xem ghi chú cũ là đánh giá code mới. |
| Một fenced Python block hoặc raw Python parse được có định nghĩa | Tránh ghép nhiều phương án hoặc tự sửa output của model. Fence syntax-invalid được giữ để tạo SyntaxError feedback. Không thêm imports/hàm hoặc gọi LLM sửa format. |
| Analyst luôn lưu notes, flag khi chứa code | Không biến response sai vai trò thành replacement implementation hoặc một dispatch bổ sung. Cờ detection là quan sát cú pháp, không phải semantic judge. |
| 6 lượt worker, dừng trước lượt 7 | Mọi generation, kể cả lỗi format/transport, đều tiêu thụ dispatch. Router STOP không tiêu thụ worker; sau lượt cuối không có STOP/final LLM ngầm. Budget exhaustion vẫn freeze/chấm candidate cuối. |
| Deadline theo monotonic clock | Không phụ thuộc múi giờ hoặc chỉnh system clock. UTC timestamp dùng audit; latency workflow không gồm load/warmup/evaluation. |
| Supervisors theo process | Laya resident process bị terminate khi quá hạn. HTTP worker có process riêng cho mỗi request để socket timeout không bị server giữ kết nối kéo dài; không retry. Docker bị `rm -f` khi quá thời gian outer supervisor. Cleanup có một khoảng thời gian giới hạn riêng. |
| Phân loại system error độc lập | Container startup/crash, HTTP, SDK/schema khác SyntaxError/AssertionError/candidate timeout. Không tính mọi lỗi hạ tầng thành model incorrect. |

### Laya và token budget

SDK **laya 0.4.37** được tải và cài vào virtualenv để đọc đúng code, thay vì giả định API trong đặc tả. Adapter gọi `laya.load` đúng model/revision, `backend=eager`, không Vega, không automatic checkpoint routing, không teacher. Default CPU; không bật compile/fast path để giảm biến số ở pilot. Tắt autocast trong adapter và ghi dtype/device thật. SDK có nhánh GPU OOM retry trên CPU; adapter biến warning trước nhánh retry này thành lỗi, giữ zero-hidden-retry policy. CUDA cần kiểm chứng riêng trước khi sử dụng kết quả.

`LayaRenderer` sử dụng chính `laya.common.build_head/build_sequence` và checkpoint tokenizer; kiểm tra toàn bộ instruction span, từng option span, marker và state token count. SDK có cap 48 token mỗi option và có thể cắt instruction; chỉ đếm marker là chưa đủ. Raw rendered IDs và hash được lưu. Không coi 256 head budget là 256 token thật; head dài bao nhiêu phụ thuộc tập 2/4 lựa chọn. Nếu SDK tiếp tục cắt sau context builder, episode bị chặn.

Mandatory view giữ đề, entry point, budget, tồn tại code, counts/status và lỗi attempt gần nhất. Error tối đa 96 token, có metadata trước/sau. Phần còn lại chia code 65%, analysis 25%, events 10%; phần không dùng trả cho code rồi analysis. Code dùng prefix:suffix 2:1 theo dòng, notes lấy prefix theo dòng, events ưu tiên ba event mới nhất. Có marker và lưu nội dung trước/sau, số token loại. Render lại cho đến khi vừa; không cắt mandatory task. Khi một dòng hoặc marker không còn chỗ, field có thể rỗng nhưng metadata vẫn ghi việc bỏ nội dung. Không có summarizer, retrieval hay tokenizer Qwen dùng thay cho Laya.

SDK trả probabilities làm tròn **4 chữ số**. Vì có tối đa 4 action, chọn tolerance tổng **0.0003** để chấp nhận sai số làm tròn (ví dụ 3 × 0.3333), không renormalize/softmax. Sai keys, NaN, giá trị âm hoặc choice ngoài legal set đều lỗi. Effective action là argmax; tie theo thứ tự analyst, implementer, repairer, stop đã lọc. Lưu cả SDK choice và effective action; confidence không điều khiển nhánh và không được coi là xác suất solve đúng.

Worker nhận full task, public examples, full code, notes/provenance, feedback và attempt error. `apply_chat_template(..., add_generation_prompt=True)` của tokenizer Qwen pin đo input; dự trữ 1024 output trong context 8192. Overflow dừng, không cắt full code. Model endpoint dùng một HTTP POST, `n=1`, không tools/streaming/retry; requested body và raw response lưu riêng, không lưu headers/key/base URL. Seed chỉ gửi nếu provider đã xác nhận hỗ trợ; effective seed/usage thiếu vẫn null.

### Public data và final evaluator

| Lựa chọn | Lý do và giới hạn |
|---|---|
| Public export có whitelist field và hash | Online loader không import EvalPlus hoặc đọc reference/base_input/plus_input. Field lạ bị từ chối thay vì bị âm thầm bỏ. Đây là ranh giới bằng module/data contract; orchestrator không được sandbox ở mức OS khỏi filesystem của người dùng. |
| Đối chiếu numeric ID, entry point, public statement, assertion | Không suy ra expected output từ reference. Original code chỉ được parse ở prepare-data để kiểm tra tên hàm/import, không đưa vào public export hoặc thực thi. |
| Bỏ URL chỉ trong phép đối chiếu | MBPP+ bỏ citation URL ở một số task gốc. Loại URL khỏi comparison tránh báo mismatch giả, nhưng public payload vẫn giữ nguyên statement gốc. Nội dung khác thực sự vẫn bị chặn. Bằng chứng quan sát trên Mbpp/749 và Mbpp/771 đã ghi exposure. |
| Sort theo ID số rồi `random.Random(42).sample(...,20)` | RNG cục bộ không bị các bước khác làm đổi seed. Lưu chính IDs và snapshot hash; không cho phép thay task lỗi bằng task dễ hơn. |
| Raw release snapshot riêng `data/private` | Giữ nguyên serialization đặc thù MBPP+, không deserialize rồi JSON dump làm biến đổi tuple/complex trước evaluator. SHA-256 pin raw bytes; release label không được dùng thay content hash. |
| Official EvalPlus 0.3.1, offline container riêng | Package version khác dataset v0.2.0. Gửi `task_id` + **full `solution`**, không nhầm với completion được nối vào prompt. |
| Subset private dataset theo frozen candidate IDs | EvalPlus 0.3.1 yêu cầu sample cho mọi problem được load. Wrapper lọc nguyên row của ID đã freeze, dùng `MBPP_OVERRIDE_PATH`, vẫn gọi evaluator/oracle gốc và giữ tất cả base/plus inputs của mỗi task. Không lọc test theo kết quả. |
| Chấm base và extended riêng | `base_pass` lấy `base_status`, `plus_pass` lấy `plus_status`; strict yêu cầu cả hai. Không đồng nhất plus với public feedback hoặc gán combined pass hai lần. |
| Không candidate là outcome false; evaluator crash là unknown/error | Không loại output-format failure khỏi denominator hợp lệ. System-error có code chỉ giữ để chẩn đoán, không trộn vào valid episodes. |

Public examples có thể trùng base tests. Protocol có feedback nhiều vòng nên không so trực tiếp với one-shot/no-feedback leaderboard. Các task đã xem để sửa kiến trúc cần đánh dấu:

Interface benchmark trả manifest, mapping task và mapping preparation errors cùng lúc, thay vì chỉ một list task như pseudocode. Lựa chọn này giữ được các ID lỗi trong audit và denominator; không cho phép `load_public_tasks` vô tình làm mất task được chọn. Executor nhận thêm timeout/log handle để bound thực thi và lưu output đúng episode.

```powershell
python -m slm_mas.cli mark-exposed --run-dir runs/<run_id> --task-id Mbpp/749 --reason 'inspected trajectory to change routing view'
```

`inspect` và `report` không gọi API, không sửa online artifacts/state. `mark-exposed` ghi ledger riêng; giữ đồng bộ registry `data/development_exposed_ids.json` cho các run mới. Online loader merge registry này vào run manifest. Seed không đảm bảo task không nằm trong pretraining.

### Sandbox và giới hạn

Public container: network none, UID 65532, cap-drop ALL, no-new-privileges, root filesystem read-only, 1 CPU/512 MiB/64 PID, tmpfs 64 MiB. Chỉ bind mount thư mục tạm gồm candidate và assertions công khai read-only; harness nằm trong image. Không mount workspace, credentials, model cache hoặc evaluator data. Runtime không cài package theo model output; image public chỉ có Python standard library.

Harness tạo process con riêng cho từng assertion, namespace mới để các test không vô tình sửa trạng thái lẫn nhau. Parent harness nhận kết quả riêng, không đọc dòng “all tests passed” trong stdout. SyntaxError được kiểm tra trước; `os._exit(0)` không có completion message không được tính pass. Output candidate giới hạn 8 KiB/stream, output Docker giữ tối đa 64 KiB và được drain để không tràn RAM. Quá hạn candidate tạo feedback; container startup/harness crash tạo system error. Startup grace mặc định 5 giây được tách với public suite budget 10 giây và ghi config. Offline evaluator được 2 GiB/128 PID vì EvalPlus cần nhiều process hơn; vẫn cùng no-network/non-root/readonly, chỉ riêng output mount được ghi.

Container và Python harness **không phải sandbox tuyệt đối hoặc harness chống gian lận có chủ đích**. Candidate cùng Python interpreter có thể introspect và can thiệp môi trường thực thi trong container; IPC cũng không phải kênh attestation mật mã. Giới hạn này phù hợp pilot coding benign, chưa đủ cho benchmark code đối kháng. Không tuyên bố test integrity trước adversarial Python chỉ nhờ read-only mount. Nếu mở rộng sang tác nhân đối kháng, cần sandbox mạnh hơn và giao thức evaluator tách đặc quyền; đó là thay đổi protocol phải version riêng.

### Log, audit và mẫu số

Mỗi run dùng UTC timestamp + UUID, không ghi đè. Config, prompts và toàn bộ source `.py` có hash; manifest lưu package versions, model revisions và source commit nếu có. Run chưa commit vẫn có source hashes để phát hiện code khác nhau. Event JSONL có sequence, timestamps, state hash và payload refs; JSON ghi bằng atomic replace, events flush mỗi lần. Không triển khai crash-resume v0; state từ run đã chấm hidden tests không quay lại loop.

```text
runs/<id>/
  run_manifest.json, config.resolved.yaml, tasks.manifest.json
  prompts/, preflight.json, preflight/
  episodes/<safe-id>/
    events.jsonl, snapshots/, router/, worker/, artifacts/, public_checks/
    final.py, episode_result.json
  evaluation/, development_exposed_ids.json
  summary.json, episode_metrics.csv, report.md
```

Filename task gồm slug + hash để tránh path traversal và collision giữa ID có dấu `/`. Lưu exact model payload benchmark, full response, finish reason, output-length flag, raw SDK choice/probabilities, artifact provenance, output refs và transitions. Không lưu toàn bộ environment hoặc exception HTTP có thể chứa secret. Binary model weights, dataset private, runs và `.env` được ignore trong Git; bằng chứng nhỏ được chép có chọn lọc vào `verification/`.

Report tính pass↔fail giữa **code versions**, không coi Analyst là regression. Có role distribution, repeated roles, unchanged code, stop-on-public-fail, budget reasons, errors/refs và truncation. Token usage thiếu là null; không biến zero-generated-text của Laya thành zero compute, không tính tiền khi chưa có pricing.

`strict_success_over_selected` dùng mọi selected ID, gồm blocked/missing/system error; `strict_success_over_valid_episodes` chỉ dùng workflow không lỗi hạ tầng và outcome xác định, vẫn giữ no-candidate/output failures. Coverage và lý do loại được công khai. Khi chưa có final evaluator, candidate có public pass vẫn là `not_run`, valid denominator có thể bằng 0 (tỷ lệ null). Với mock, tỷ lệ over-selected chỉ là thống kê vận hành synthetic, không phải điểm MBPP+.

## Phạm vi kiểm chứng và nguồn

Tests tập trung vào invariant làm sai thực nghiệm: legal actions, handoff, parsing, budget, provenance, private sentinel, truncation/overflow, distribution validation, sandbox mount contract, freeze hash, denominator và mock/real boundary. Docker execution, HTTP provider thật và model inference cần integration trên hạ tầng tương ứng; không thể chứng minh chúng chỉ bằng mock unit tests.

Nguồn API đã đối chiếu: [Laya SDK](https://github.com/NandhaKishorM/laya), [checkpoint Laya-TD](https://huggingface.co/convaiinnovations/laya-typed-decisions), [EvalPlus](https://github.com/evalplus/evalplus), [MBPP loader](https://github.com/evalplus/evalplus/blob/master/evalplus/data/mbpp.py), [Qwen checkpoint](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct). Code wheel đã pin là căn cứ adapter; model card không phải bằng chứng routing hiệu quả trên coding MAS. Không có reviewer, screening, fine-tuning, router khác hoặc benchmark khác trong v0.
