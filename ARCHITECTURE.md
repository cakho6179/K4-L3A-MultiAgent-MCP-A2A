# L3A Architecture Record

Hệ thống multi-agent điều tra khiếu nại thương mại điện tử cho variant `l3a`.
Tài liệu mô tả quyết định có thể kiểm chứng; không chứa prompt hay chain-of-thought.

## 1. System overview

```text
inputs/<case_id>.json
        │
        ▼
   coordinator ──task_assigned──┬─► order-agent     get_order, get_order_items
        │                       ├─► payment-agent   get_order_payments, get_payment_timeline,
        │                       │                   get_refund_timeline
        │                       ├─► shipment-agent  get_shipment_summary, get_sellers
        │                       └─► policy-agent    get_policy
        │                                │
        │                    (MCP Evidence Gateway — evidence_ref + result_hash)
        │                                │
        │                                ▼
        │                       AuthoritativeView  ◄── loại bỏ decoy
        │                                │
        │                          policy engine ──policy_decided──► verifier-agent
        │                                                                │
        └──────────────────── case_finalized ◄──verification_completed───┘
                                     │
                    outputs/<case_id>.json + traces/trace.jsonl
```

Điểm cốt lõi: **mọi giá trị trong output đều truy nguyên về một MCP envelope đã
validate**. Không có nhánh nào sinh dữ liệu suy đoán, và không có nhánh nào tạo
`evidence_ref`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| `coordinator` | `inputs/<case_id>.json` | Mở case, phát `task_assigned`, đóng case | `case_finalized` |
| `order-agent` | `claimed_order_id` | Lấy order row thẩm quyền + item rows; chốt `order_total` | → `payment-agent` |
| `payment-agent` | `order_id` | Capture events, reconciliation, refund lifecycle | → `shipment-agent` |
| `shipment-agent` | `order_id` | Mốc giao hàng, `shipping_limit`, danh tính seller | → `policy-agent` |
| `policy-agent` | AuthoritativeView | Phân loại `primary_issue`, tra `EC_POLICY_V1` | `policy_decided` → `verifier-agent` |
| `verifier-agent` | Output nháp | Bất biến chéo trường, hiệu chuẩn confidence | `verification_completed` |

**Tool permissions** được khai báo tĩnh tại `AGENT_TOOLS` trong `workflow.py` và
cưỡng chế lúc chạy: `Specialist.consume()` raise `ToolPermissionError` nếu một
actor gọi tool ngoài scope. `verifier-agent` không có quyền gọi tool nào —
nó chỉ được đọc state đã thu thập. `get_product_context` và `get_customer_history`
không nằm trong scope của agent nào vì không đóng góp cho bất kỳ verdict nào;
trích dẫn chúng chỉ làm giảm evidence precision.

## 3. A2A protocol

- **Envelope**: `CaseState` (`workflow.py`) — `case_id`, `order_id`,
  `policy_version`, `payloads`, `refs_by_domain`, `refs_by_tool`, `unavailable`.
- **Correlation**: mọi trace event và mọi MCP call đều mang `case_id`; gateway
  từ chối (403) nếu `case_id` không khớp scope.
- **Handoff**: tuyến tính một chiều
  `order → payment → shipment → policy → verifier`. Không có cạnh ngược, nên
  **không thể có vòng lặp** — độ sâu graph là hằng số 5.
- **Timeout/retry**: xem mục 5.
- Trace chỉ ghi event type và decision code quan sát được; không ghi nội dung suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call()` validate envelope theo
   `mcp-evidence-response-v1.schema.json` trước khi trả về. Envelope sai schema
   → `ContractError`, không bao giờ vào state.
2. `Specialist.consume()` lưu `evidence_ref` vào `refs_by_domain[domain]` và phát
   `tool_result_consumed` kèm đúng ref đó.
3. `build_view()` dựng **AuthoritativeView** — xem mục 4.1.
4. `EVIDENCE_PLAN[primary_issue]` chọn các domain thực sự chống đỡ kết luận;
   chỉ ref của các domain đó được trích dẫn trong `evidence_refs`.
5. `verify()` cắt mọi ref trong `claim_assessments` không nằm trong
   `evidence_refs` cấp case.

Evidence không tái sử dụng giữa các case: `refs_by_domain` nằm trong `CaseState`,
khởi tạo mới mỗi `solve_case()`.

### 4.1 Decoy filtering — phần quyết định của bài

Generator gắn cho mỗi case **một kịch bản thẩm quyền + một kịch bản decoy** lệch
mốc thời gian. Bốn mỏ neo tách chúng:

| Nguồn | Quy tắc giữ lại bản thẩm quyền |
| --- | --- |
| `order_items` | `shipping_limit_date` nhỏ nhất trong các mốc `>= order_purchase_timestamp` |
| `payment_timeline.events` | `event_at` cùng ngày lịch với `order_approved_at` |
| `refund_timeline.events` | `amount_brl` khớp một capture thẩm quyền **và** `event_at >= order_approved_at` |
| `shipment.events` | `event_at == order_delivered_customer_date` |

Mỗi nguồn bị loại đều được báo cáo trong `data_conflicts` với `resolution_code`
tương ứng (`ANCHORED_TO_ORDER_PURCHASE`, `ANCHORED_TO_ORDER_APPROVED_AT`,
`REFUND_MATCHED_TO_CAPTURE`, `ANCHORED_TO_ORDER_DELIVERY`) — không giấu dữ liệu
đã bỏ.

### 4.2 Thứ tự phân loại

`policy.classify()` áp dụng theo thứ tự ưu tiên — tín hiệu tường minh trước,
suy luận số học sau:

1. `order_status == canceled` + có capture → `canceled_order_paid`
2. `order_status == unavailable` + có capture → `unavailable_order_paid`
3. Có `reconciliation_mismatch` thẩm quyền → `payment_mismatch`
4. Refund thẩm quyền `status == failed` → `refund_failed`
5. Refund thẩm quyền `status == pending` → `refund_pending`
6. Các capture bằng nhau: tổng `== order_total` → `valid_split_payment`;
   tổng `> order_total` → `duplicate_charge`
7. Giao trễ → `late_delivery_seller` / `late_delivery_logistics`
   (theo `actor` của shipment event; fallback: `carrier_at > shipping_limit_at`)
8. Không bất thường → `unsupported_claim`
9. Không có order row → `insufficient_evidence`

`case_status`, `recommended_action`, `recommended_refund_brl` và
`responsible_parties[].party_type` **tra thẳng** từ `get_policy`. Ngoại lệ duy
nhất: khi `party_type == "seller"`, `party_id` được ràng buộc lại về seller của
chính order này.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi mạng (`TimeoutError`, `OSError`) | Có — 3 lần, backoff tuyến tính 0.5s×n | Tool optional → coi như thiếu dữ liệu; tool bắt buộc → fail case | `tool_result_consumed` / `EVIDENCE_UNAVAILABLE` |
| Gateway báo lỗi tool (`RuntimeError: Error executing tool`) | Có — 3 lần, backoff tuyến tính (server flaky theo đợt) | Như trên | `tool_result_consumed` / `EVIDENCE_UNAVAILABLE` |
| Envelope sai schema (`ValueError`) | Có — có thể do payload hỏng dọc đường | Như trên | `tool_result_consumed` / `EVIDENCE_UNAVAILABLE` |
| Thiếu order row | Không | `primary_issue = insufficient_evidence`, confidence 0.55 | `policy_decided` / `NO_AUTHORITATIVE_ORDER` |
| Thiếu policy rule | Không | `needs_investigation`, refund 0, confidence ≤ 0.5 | `policy_decided` / `POLICY_RULE_UNAVAILABLE` |
| Xung đột nguồn | Không | Neo theo mục 4.1, ghi `data_conflicts` | `verification_completed` |
| Kết quả specialist sai bất biến | Không | Verifier sửa tất định + hạ confidence | `verification_completed` / mã repair |

`OPTIONAL_TOOLS = {get_refund_timeline, get_sellers, get_product_context}`:
vắng mặt là kết quả hợp lệ, không phải lỗi. Retry idempotent — mọi tool đều là read-only.

**Không bao giờ** chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

`verifier.verify()` kiểm tra trước finalize:

1. **Schema** — output validate theo `l3a-output-v2.schema.json`.
2. **Money ≥ 0** — refund âm bị kẹp về 0 (`NEGATIVE_REFUND_CLAMPED`).
3. **Status ↔ refund** — `no_action` không được mang tiền (`NO_ACTION_REFUND_DROPPED`);
   refund > 0 buộc `action_required` (`REFUND_FORCED_ACTION_REQUIRED`).
4. **Money totals** — `Σ refund_lines[].amount_brl == recommended_refund_brl`
   (`REFUND_LINES_REBALANCED`).
5. **Seller responsibility** — `party_id` khi `party_type == "seller"` phải thuộc
   `affected_entities.seller_ids` (`SELLER_IDENTITY_REBOUND`).
6. **Claim linkage** — `claim_assessments[].evidence_refs ⊆ evidence_refs`.
7. **Entity scope** — mọi id trích từ payload MCP của chính case đó.
8. **Confidence bounds** — kẹp về `[0, 1]`, làm tròn 2 chữ số.

### Confidence calibration

| Nhóm | Confidence | Lý do |
| --- | --- | --- |
| `canceled`, `unavailable` | 0.93 | Đọc thẳng từ order row |
| `reconciliation_mismatch`, refund status | 0.92 | Một event quyết định |
| Giao trễ có `actor` | 0.90 | Event chỉ đích danh bên chịu trách nhiệm |
| split / duplicate | 0.87 | Phụ thuộc `order_total` đã chọn |
| `unsupported_claim` | 0.78 | Suy từ sự vắng mặt tín hiệu |
| `insufficient_evidence` | 0.55 | Không đủ dữ liệu |

## 7. Reproducibility

- **Không dùng LLM** ở runtime. Pipeline là state machine async thuần Python,
  tất định: cùng input + cùng evidence → cùng output. Không random seed.
- **Concurrency**: 1 — các case chạy tuần tự trong một `ClientSession` MCP duy
  nhất. 8 MCP call/case, 800 call/run.
- **Dependency pinning**: `pyproject.toml` (`httpx2>=2,<3`, `mcp>=2,<3`,
  `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`); Python 3.11.
- **Lệnh chạy**:
  ```bash
  day09 validate-inputs
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
- **Giới hạn tài nguyên**: MCP timeout 300s, 3 lần retry, backoff tuyến tính.
- **Credentials**: chỉ đọc từ `.env` qua `Settings.load()`.

### Ghi chú tương thích

`mcp_gateway.py` hỗ trợ cả `mcp` 1.x (`result.isError`, `structuredContent`) và
2.x (`result.is_error`, `structured_content`) — giữ tương thích ngược.
