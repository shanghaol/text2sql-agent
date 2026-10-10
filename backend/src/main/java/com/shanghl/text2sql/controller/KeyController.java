package com.shanghl.text2sql.controller;

import com.shanghl.text2sql.common.ApiResponse;
import com.shanghl.text2sql.dto.KeyPutRequest;
import com.shanghl.text2sql.dto.KeyPutResult;
import com.shanghl.text2sql.dto.KeyStatus;
import com.shanghl.text2sql.dto.VerifyKeyResult;
import com.shanghl.text2sql.security.CurrentUser;
import com.shanghl.text2sql.service.KeyService;

import jakarta.validation.Valid;

import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * BYOK 四接口（设计 §2.4 / §1.2）。
 * 请求体含明文 key：本类与 KeyService 的任何日志都不打请求体（§1.5 规则 1），
 * key 只在 POST/PUT body 中传输、绝不进 URL（规则 3）。
 */
@RestController
public class KeyController {

    private final KeyService keyService;

    public KeyController(KeyService keyService) {
        this.keyService = keyService;
    }

    /** 录入：格式预检 → GET /models 同步校验（5s）→ AES-GCM 加密 upsert → 返回掩码+指纹。 */
    @PutMapping("/api/user/key")
    public ApiResponse<KeyPutResult> put(@Valid @RequestBody KeyPutRequest req,
                                         @AuthenticationPrincipal CurrentUser user) {
        return ApiResponse.ok(keyService.put(user.id(), req.key()));
    }

    /** 查询：只回掩码/指纹/时间——任何接口永不返回明文 key（§1.5）。 */
    @GetMapping("/api/user/key")
    public ApiResponse<KeyStatus> get(@AuthenticationPrincipal CurrentUser user) {
        return ApiResponse.ok(keyService.get(user.id()));
    }

    /** 复检：对已存 key 重跑 /models（设置页"重新校验"按钮；5 次/分限流）。 */
    @PostMapping("/api/user/key/verify")
    public ApiResponse<VerifyKeyResult> verify(@AuthenticationPrincipal CurrentUser user) {
        return ApiResponse.ok(keyService.verifyStored(user.id()));
    }

    /** 删除：硬删行；删除后对话返回 key_not_set（§2.4）。 */
    @DeleteMapping("/api/user/key")
    public ResponseEntity<Void> delete(@AuthenticationPrincipal CurrentUser user) {
        keyService.delete(user.id());
        return ResponseEntity.noContent().build(); // 204
    }
}
