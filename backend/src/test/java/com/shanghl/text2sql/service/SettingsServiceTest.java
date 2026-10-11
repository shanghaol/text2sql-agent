package com.shanghl.text2sql.service;

import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.conditions.Wrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.dto.UserSettingsView;
import com.shanghl.text2sql.entity.User;
import com.shanghl.text2sql.mapper.UserMapper;

import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.Mockito;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.verify;

/**
 * SettingsService 切片单测（W5 §3.8）：mock UserMapper，不起 Spring、不连 MySQL。
 * 覆盖：get 缺行/0/1 三态、update 写列与返回值、用户行消失的 AUTH_REQUIRED 兜底。
 */
class SettingsServiceTest {

    private UserMapper userMapper;
    private SettingsService service;
    private User row;

    @BeforeEach
    void setUp() {
        // lambda wrapper 需要 TableInfo 缓存（与 ChatProxyServiceTest 同款处理）
        TableInfoHelper.initTableInfo(new MapperBuilderAssistant(new MybatisConfiguration(), ""), User.class);
        userMapper = Mockito.mock(UserMapper.class);
        service = new SettingsService(userMapper);
        row = new User();
        row.setId(42L);
        row.setUsername("tester");
        row.setHitlEnabled(0);
        doReturn(row).when(userMapper).selectById(Mockito.anyLong());
    }

    @Test
    @DisplayName("get：hitl_enabled=0/1 → false/true；行缺失 → auth_required")
    void getSemantics() {
        assertThat(service.get(42L)).isEqualTo(new UserSettingsView(false));
        row.setHitlEnabled(1);
        assertThat(service.get(42L)).isEqualTo(new UserSettingsView(true));
        // null 视为关（新列历史行理论不会有 null，防御兜底）
        row.setHitlEnabled(null);
        assertThat(service.get(42L)).isEqualTo(new UserSettingsView(false));

        doReturn(null).when(userMapper).selectById(Mockito.anyLong());
        assertThatThrownBy(() -> service.get(42L))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("auth_required"));
    }

    @Test
    @DisplayName("update：true→写 1、false→写 0；仅 set hitl_enabled 列；返回更新后视图")
    void updateWritesColumn() {
        UserSettingsView view = service.update(42L, true);
        assertThat(view).isEqualTo(new UserSettingsView(true));

        ArgumentCaptor<Wrapper<User>> captor = ArgumentCaptor.forClass(Wrapper.class);
        verify(userMapper).update(any(), captor.capture());
        LambdaUpdateWrapper<User> wrapper = (LambdaUpdateWrapper<User>) captor.getValue();
        assertThat(wrapper.getSqlSet()).contains("hitl_enabled"); // 只动这一列
        assertThat(wrapper.getSqlSet()).doesNotContain("password_hash");

        doAnswer(inv -> {
            row.setHitlEnabled(0); // 模拟 DB 行已更新
            return 1;
        }).when(userMapper).update(any(), any());
        assertThat(service.update(42L, false)).isEqualTo(new UserSettingsView(false));
    }

    @Test
    @DisplayName("update：用户行消失 → auth_required（JWT 已验身份的异常态兜底）")
    void updateMissingUserRejected() {
        doReturn(null).when(userMapper).selectById(Mockito.anyLong());
        assertThatThrownBy(() -> service.update(42L, true))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("auth_required"));
    }
}
