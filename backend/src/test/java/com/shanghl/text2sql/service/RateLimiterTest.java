package com.shanghl.text2sql.service;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** 固定窗口限流单测：窗口内超限拒绝、窗口过期恢复、不同 key 互不影响（不起 Spring）。 */
class RateLimiterTest {

    private static final long MINUTE = 60_000L;

    private final RateLimiter limiter = new RateLimiter();

    @Test
    @DisplayName("窗口内达到上限后拒绝（对话 10/min 场景的缩小版）")
    void rejectsBeyondLimitWithinWindow() {
        long t0 = 1_200_000L; // 必须窗口对齐（t0 % 60000 == 0），窗口起点 = t0
        assertThat(limiter.tryAcquire("chat:42", 3, MINUTE, t0)).isTrue();
        assertThat(limiter.tryAcquire("chat:42", 3, MINUTE, t0 + 1)).isTrue();
        assertThat(limiter.tryAcquire("chat:42", 3, MINUTE, t0 + 2)).isTrue();
        assertThat(limiter.tryAcquire("chat:42", 3, MINUTE, t0 + 3)).isFalse(); // 第 4 次超限
        assertThat(limiter.tryAcquire("chat:42", 3, MINUTE, t0 + MINUTE - 1)).isFalse(); // 窗口尾仍拒绝
    }

    @Test
    @DisplayName("窗口过期后恢复可用")
    void recoversAfterWindowExpires() {
        long t0 = 2_400_000L; // 窗口对齐
        for (int i = 0; i < 5; i++) {
            assertThat(limiter.tryAcquire("chat:7", 5, MINUTE, t0 + i)).isTrue();
        }
        assertThat(limiter.tryAcquire("chat:7", 5, MINUTE, t0 + MINUTE - 1)).isFalse();
        // 跨过窗口边界（新窗口起点 = now - now % window）
        assertThat(limiter.tryAcquire("chat:7", 5, MINUTE, t0 + MINUTE)).isTrue();
        assertThat(limiter.tryAcquire("chat:7", 5, MINUTE, t0 + MINUTE + 1)).isTrue();
    }

    @Test
    @DisplayName("不同 key 独立计数（不同用户 / 不同 IP）")
    void independentPerKey() {
        long t0 = 3_600_000L;
        assertThat(limiter.tryAcquire("chat:1", 1, MINUTE, t0)).isTrue();
        assertThat(limiter.tryAcquire("chat:1", 1, MINUTE, t0 + 1)).isFalse();
        assertThat(limiter.tryAcquire("chat:2", 1, MINUTE, t0 + 2)).isTrue(); // 另一个用户不受影响
    }

    @Test
    @DisplayName("check() 超限抛 BizException(rate_limited) 且带 Retry-After 语义的错误码")
    void checkThrowsBizException() {
        RateLimiter limiter = new RateLimiter();
        limiter.check("k", ErrorCode.RATE_LIMITED, 1, MINUTE); // 第 1 次通过
        assertThatThrownBy(() -> limiter.check("k", ErrorCode.RATE_LIMITED, 1, MINUTE))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("rate_limited"));
        // 换错误码（verify_rate_limited）同样生效
        RateLimiter other = new RateLimiter();
        assertThatCode(() -> other.check("v", ErrorCode.VERIFY_RATE_LIMITED, 5, MINUTE))
                .doesNotThrowAnyException();
    }
}
