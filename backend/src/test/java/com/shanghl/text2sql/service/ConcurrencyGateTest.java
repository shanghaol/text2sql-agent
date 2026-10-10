package com.shanghl.text2sql.service;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 全局并发闸单测（§1.7 / §8 T1 验收"并发闸第 5 个请求 503"的逻辑等价验证）：
 * 上限 4 → 第 5 个被拒；释放后可再进；多线程压测下计数永不越界、终值归零。
 */
class ConcurrencyGateTest {

    @Test
    @DisplayName("上限 4：第 5 个进入失败；exit 后可再进")
    void fifthEnterRejectedThenRecovered() {
        ConcurrencyGate gate = new ConcurrencyGate(4);
        List<ConcurrencyGate> entered = new ArrayList<>();
        for (int i = 0; i < 4; i++) {
            assertThat(gate.tryEnter()).as("第 %d 个应成功", i + 1).isTrue();
        }
        assertThat(gate.tryEnter()).isFalse(); // 第 5 个 → 503 concurrency_full
        assertThat(gate.inFlight()).isEqualTo(4);

        gate.exit(); // 模拟一条流结束
        assertThat(gate.inFlight()).isEqualTo(3);
        assertThat(gate.tryEnter()).isTrue(); // 释放后可再进
    }

    @Test
    @DisplayName("tryEnter 失败路径不泄漏名额（拒绝本身已自减）")
    void rejectedEnterDoesNotLeak() {
        ConcurrencyGate gate = new ConcurrencyGate(1);
        assertThat(gate.tryEnter()).isTrue();
        assertThat(gate.tryEnter()).isFalse();
        assertThat(gate.tryEnter()).isFalse();
        assertThat(gate.inFlight()).isEqualTo(1); // 拒绝不占名额
        gate.exit();
        assertThat(gate.inFlight()).isEqualTo(0);
        assertThat(gate.tryEnter()).isTrue();
    }

    @Test
    @DisplayName("多线程压测：成功进入次数 == max，全部退出后归零")
    void concurrentHamster() throws Exception {
        ConcurrencyGate gate = new ConcurrencyGate(4);
        int threads = 64;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        List<Boolean> results = java.util.Collections.synchronizedList(new ArrayList<>());
        for (int i = 0; i < threads; i++) {
            pool.submit(() -> {
                try {
                    start.await();
                    boolean ok = gate.tryEnter();
                    results.add(ok);
                    if (ok) {
                        gate.exit();
                    }
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    done.countDown();
                }
            });
        }
        start.countDown();
        assertThat(done.await(10, TimeUnit.SECONDS)).isTrue();
        pool.shutdownNow();

        long successCount = results.stream().filter(Boolean::booleanValue).count();
        assertThat(successCount).isLessThanOrEqualTo(4);
        assertThat(gate.inFlight()).isLessThanOrEqualTo(4);
        assertThat(gate.inFlight()).isEqualTo(0); // 全部退出后归零（无泄漏）
    }
}
