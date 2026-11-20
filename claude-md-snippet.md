## Token 追蹤 tag 規則

這個專案用 `task_tokens.py` 依 tag 計算每個 task 的 token 用量。請遵守以下規則：

- 依 spec 派工時，session 的第一句 prompt 以 `[spec名稱]` 開頭，例如 `[checkout] 依 spec.md 派工`。
- 派 sub-agent（Agent tool）時，`description` 必須以 `[spec名稱/task-id]` 開頭，例如 `[checkout/T3] implement 付款 API`。
- review 與修改沿用同一個 task-id，並加上階段：`[checkout/T3/review]`、`[checkout/T3/fix]`。
- sub-agent 再派 sub-agent 時，沿用自己的 tag。
- tag 只能用英數字、`-`、`_`、`.`，段與段之間用 `/` 隔開，而且必須放在最開頭。
- 一份 spec 裡的 task-id 要固定，不要中途改名（例如 T3 不要改成 task3）。
