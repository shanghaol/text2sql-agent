// @vitest-environment happy-dom
/**
 * ApprovalCard 渲染分支（W5 §3.7）：@vue/test-utils 挂载，断言三种 pending 状态下的
 * 卡片表现——正常（historyId 绑定后按钮可用 + emit）、未绑定（禁用）、过期（置灰态）。
 * el-button 用最小桩（本项目不挂 element-plus 插件，桩透传 disabled 与 click）。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ApprovalCard from './ApprovalCard.vue'

const pending = (overrides = {}) => ({
  approvalId: 'a-1',
  sql: 'SELECT SUM(amount) FROM payments',
  historyId: 77,
  expired: false,
  busy: false,
  resolved: false,
  ...overrides,
})

const mountCard = (p) =>
  mount(ApprovalCard, {
    props: { pending: p },
    global: {
      stubs: {
        'el-button': {
          props: ['disabled', 'loading'],
          emits: ['click'],
          template:
            '<button :disabled="disabled" :data-loading="loading ? \'1\' : null" @click="$emit(\'click\')"><slot /></button>',
        },
      },
    },
  })

describe('ApprovalCard 渲染分支（W5 §3.7）', () => {
  it('正常态：SQL 只读展示 + 批准/拒绝按钮，点击 emit approve/reject', async () => {
    const w = mountCard(pending())
    expect(w.find('.sql').text()).toBe('SELECT SUM(amount) FROM payments')
    expect(w.find('.badge.waiting').text()).toBe('等待您确认')
    const buttons = w.findAll('button')
    expect(buttons.map((b) => b.text())).toEqual(['批准执行', '拒绝'])
    await buttons[0].trigger('click')
    expect(w.emitted('approve')).toHaveLength(1)
    await buttons[1].trigger('click')
    expect(w.emitted('reject')).toHaveLength(1)
  })

  it('pending_saved 未到（historyId=null）：按钮禁用，阻止无 historyId 的批准请求', () => {
    const w = mountCard(pending({ historyId: null }))
    const buttons = w.findAll('button')
    expect(buttons[0].attributes('disabled')).toBeDefined()
    expect(buttons[1].attributes('disabled')).toBeDefined()
  })

  it('busy：批准按钮进入 loading（防双击），拒绝禁用', () => {
    const w = mountCard(pending({ busy: true }))
    const buttons = w.findAll('button')
    // loading 由 el-button 真组件处理；桩上透传为 data-loading（loading 的按钮不可点）
    expect(buttons[0].attributes('data-loading')).toBe('1')
    expect(buttons[1].attributes('disabled')).toBeDefined()
  })

  it('过期态（409/410 响应后）：无按钮、显示置灰提示与"已过期"徽标', () => {
    const w = mountCard(pending({ expired: true }))
    expect(w.findAll('button')).toHaveLength(0)
    expect(w.find('.badge.expired').text()).toBe('已过期')
    expect(w.text()).toContain('审批已失效，请重新提问')
    expect(w.find('.approval-card').classes()).toContain('expired')
  })

  it('sql 缺失防御：显示占位注释而不是空块', () => {
    const w = mountCard(pending({ sql: '' }))
    expect(w.find('.sql').text()).toContain('（后端未提供 SQL）')
  })

  it('多 action 降级文案（W7 §3.1）：actionCount>1 显示"共 N 条 SQL：批准第 1 条、拒绝其余"；缺省/1 不显示', () => {
    const multi = mountCard(pending({ actionCount: 3 }))
    const note = multi.find('.multi-note')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('本轮共 3 条 SQL')
    expect(note.text()).toContain('批准第 1 条、拒绝其余')
    expect(note.text()).toContain('助手会重新发起并再次请求审批')

    expect(mountCard(pending({ actionCount: 1 })).find('.multi-note').exists()).toBe(false)
    expect(mountCard(pending({})).find('.multi-note').exists()).toBe(false) // 旧事件无该字段
  })
})
