<template>
  <section class="scenario-panel" data-testid="currency-migration-panel">
    <div class="panel-head">
      <h3>版本目录 · 旧汇率快照迁移与新提供方生效</h3>
      <span class="caliber-badge" :data-caliber="caliber" data-testid="caliber-badge">
        当前口径：{{ caliber === 'new' ? '新版（N10/7.2）' : '旧环境（L09/7.0）' }}
      </span>
    </div>

    <p class="panel-tip">
      旧快照与新提供方均版本化寻址；迁移回执可补核来源，重复回填幂等，切换失败可回滚旧环境。
    </p>

    <div class="btn-row">
      <button class="btn primary" type="button" data-testid="btn-run-migration" @click="runMigration()">
        迁移旧快照到版本目录
      </button>
      <button class="btn" type="button" data-testid="btn-migration-interrupt" @click="runInterrupted">
        模拟迁移中断
      </button>
      <button class="btn" type="button" data-testid="btn-backfill" @click="backfill">
        重复回填（幂等）
      </button>
      <button class="btn" type="button" data-testid="btn-reconcile" @click="runReconcile">
        新旧对账
      </button>
      <button class="btn warn" type="button" data-testid="btn-simulate-switch" @click="simulateSwitch">
        模拟 N10 切换失败并回滚
      </button>
      <button class="btn" type="button" data-testid="btn-rollback" @click="rollback">
        回滚旧环境（恢复 7.0）
      </button>
      <button class="btn ghost" type="button" data-testid="btn-refresh-versions" @click="loadAll">
        刷新
      </button>
    </div>

    <p v-if="message" class="panel-message" data-testid="migration-message" :data-ok="messageOk">
      {{ message }}
    </p>

    <!-- 不同版本的换算快照：7.0 旧来源 / 7.2 N10 -->
    <h4 class="block-title">同币对不同版本快照（2026-08-09 · USD/CNY）</h4>
    <div class="version-grid" data-testid="version-list">
      <article
        v-for="item in versions"
        :key="item.entry_key"
        class="version-card"
        :data-testid="`version-card-${item.provider_code || item.entry_key}`"
        :data-effective="item.effective"
        :data-kind="item.source_kind"
      >
        <header>
          <strong>{{ item.version_label || item.entry_key }}</strong>
          <span class="status-tag" :data-status="item.status">{{ item.status }}</span>
        </header>
        <div class="rate-line">
          <span class="rate-value">{{ item.rate_text || item.rate }}</span>
          <span class="rate-pair">{{ item.pair }} · {{ item.date }}</span>
        </div>
        <p class="source-line" data-testid="version-source">{{ item.source }}</p>
        <p class="key-line">关联键：<code>{{ item.entry_key }}</code></p>
        <p v-if="item.warning" class="warn-line">{{ item.warning }}</p>
        <div class="card-actions">
          <button
            class="link"
            type="button"
            :data-testid="`version-activate-${item.row_id}`"
            @click="activate(item.row_id)"
          >
            生效此版本
          </button>
        </div>
      </article>
    </div>

    <!-- 迁移回执 + 补核来源 -->
    <div v-if="receipt" class="receipt-box" data-testid="migration-receipt" :data-status="receipt.status">
      <h4 class="block-title">
        换算迁移回执 <code data-testid="receipt-id">{{ receipt.receipt_id }}</code>
        <span class="status-tag" :data-status="receipt.status">{{ receipt.status }}</span>
      </h4>
      <p class="receipt-counts" data-testid="receipt-counts">
        数据库 {{ receipt.total_in_db }} 条 / 已处理 {{ receipt.processed }} /
        新建版本 {{ receipt.created }} / 幂等更新 {{ receipt.updated }} /
        库中保留 {{ receipt.db_rows_retained }} 条（附注币对与日期不删）/
        待补核来源 {{ receipt.source_review_count }} 条
      </p>
      <ul class="review-list" data-testid="source-review-list">
        <li
          v-for="review in receipt.source_review"
          :key="review.association_key"
          :data-testid="`review-${review.association_key}`"
          class="review-item"
          :data-verified="review.verified ? 'true' : 'false'"
        >
          <div class="review-meta">
            <strong>{{ review.pair }} · {{ review.date }} · 汇率 {{ review.rate }}</strong>
            <span>{{ review.reason }}</span>
            <code>{{ review.association_key }}</code>
            <em v-if="review.verified" class="verified-line">已补核：{{ review.verified_source }}</em>
          </div>
          <div class="review-form">
            <input
              v-model="sourceInputs[review.association_key]"
              :data-testid="`source-input-${review.association_key}`"
              class="source-input"
              placeholder="补核来源（不得填成 N10，也不得编造）"
            />
            <button
              class="btn"
              type="button"
              :data-testid="`source-verify-${review.association_key}`"
              @click="verifySource(review)"
            >
              补核来源
            </button>
          </div>
        </li>
      </ul>
    </div>

    <!-- 对账结果 -->
    <div v-if="recon" class="reconcile-box" data-testid="reconcile-result" :data-ok="recon.ok">
      <h4 class="block-title">新旧对账</h4>
      <p :class="recon.ok ? 'ok-line' : 'warn-line'">{{ recon.message }}</p>
      <p class="receipt-counts">
        库 {{ recon.db_count }} 条 / 版本文件 {{ recon.catalog_file_count }} 个 /
        匹配 {{ recon.matched_count }} / 差异 {{ recon.mismatches.length }} /
        重复生效币对 {{ recon.duplicate_effective_pairs.length }}
      </p>
      <pre class="json-pre">{{ JSON.stringify(recon.db_effective_pointers, null, 2) }}</pre>
    </div>

    <!-- 按日期+币对/金额试算 -->
    <div class="convert-box" data-testid="convert-box">
      <h4 class="block-title">按旧导出的日期+币对定位并试算</h4>
      <label>金额
        <input v-model="convertForm.amount" data-testid="convert-amount" type="number" />
      </label>
      <label>日期
        <input v-model="convertForm.date" data-testid="convert-date" />
      </label>
      <label>币对
        <input v-model="convertForm.pair" data-testid="convert-pair" />
      </label>
      <button class="btn primary" type="button" data-testid="convert-run" @click="runConvert">
        定位并换算
      </button>
      <button class="btn ghost" type="button" data-testid="convert-missing-source"
              @click="checkMissingSource">
        缺来源旧记录示例（GBP）
      </button>
      <p v-if="convertResult" class="panel-message" data-testid="convert-result" :data-ok="convertResult.ok">
        <template v-if="convertResult.ok">
          {{ convertResult.amount }} × {{ convertResult.rate }} =
          <strong>{{ convertResult.converted }}</strong>
          （{{ convertResult.pair }}，来源：{{ convertResult.source }}）
        </template>
        <template v-else>不能消费：{{ convertResult.reason || convertResult.message }}</template>
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'

import { q, scenarioGet, scenarioPost } from '@/api/scenario-service'

const props = defineProps<{ tick?: number }>()
const emit = defineEmits<{ (event: 'changed'): void }>()

watch(() => props.tick, () => { void loadAll() })

const versions = ref<any[]>([])
const receipt = ref<any>(null)
const recon = ref<any>(null)
const caliber = ref<string>('legacy')
const message = ref('')
const messageOk = ref(true)
const sourceInputs = reactive<Record<string, string>>({})
const convertForm = reactive({ amount: 100, date: '2026-08-09', pair: 'USD/CNY' })
const convertResult = ref<any>(null)

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

async function loadVersions() {
  const data = await scenarioGet(`/currency_conv/versions${q({ date: '2026-08-09', pair: 'USD/CNY' })}`)
  if (data?.versions) {
    versions.value = data.versions
    caliber.value = data.active_caliber || 'legacy'
  }
}

async function loadReceipt() {
  const data = await scenarioGet('/currency_conv/migration/receipt')
  if (data?.ok) receipt.value = data.receipt || data
}

async function loadAll() {
  await Promise.all([loadVersions(), loadReceipt()])
}

async function runMigration(failAfter?: number) {
  const body: Record<string, any> = {}
  if (failAfter !== undefined) body.fail_after = failAfter
  const data = await scenarioPost('/currency_conv/migrate', body)
  flash(data.message || '迁移已执行', data.ok !== false)
  receipt.value = data.receipt_id ? data : receipt.value
  await loadAll()
  emit('changed')
}

function runInterrupted() {
  return runMigration(2)
}

async function backfill() {
  const data = await scenarioPost('/currency_conv/backfill', {})
  flash(data.message || '回填完成', true)
  receipt.value = data.receipt_id ? data : receipt.value
  await loadAll()
}

async function runReconcile() {
  recon.value = await scenarioGet('/currency_conv/reconcile')
  flash(recon.value.message || '对账完成', recon.value.ok !== false)
}

async function rollback() {
  const data = await scenarioPost('/currency_conv/rollback', { reason: '页面模拟回滚检查刚交付的附注' })
  flash(data.message || '已回滚旧环境', data.ok !== false)
  caliber.value = 'legacy'
  await loadVersions()
  emit('changed')
}

async function simulateSwitch() {
  const data = await scenarioPost('/currency_conv/simulate-failed-switch', {})
  flash(data.message || '演练完成', data.ok !== false)
  caliber.value = 'legacy'
  await loadAll()
  emit('changed')
}

async function activate(rowId: number | string) {
  const data = await scenarioPost(`/currency_conv/${rowId}/actions`, { action: '生效快照' })
  flash(data.message || '快照已生效', data.ok !== false)
  if (data.active_caliber) caliber.value = data.active_caliber
  await loadVersions()
  emit('changed')
}

async function verifySource(review: any) {
  const source = (sourceInputs[review.association_key] || '').trim()
  if (!source) {
    flash('请先填写补核来源', false)
    return
  }
  const data = await scenarioPost('/currency_conv/verify-source', {
    association_key: review.association_key,
    source,
  })
  flash(data.message || '来源已补核', data.ok !== false)
  await loadReceipt()
  await loadVersions()
  emit('changed')
}

async function runConvert() {
  convertResult.value = await scenarioGet(
    `/currency_conv/convert${q({
      amount: convertForm.amount,
      date: convertForm.date,
      pair: convertForm.pair,
    })}`,
  )
}

async function checkMissingSource() {
  convertForm.date = '2026-07-15'
  convertForm.pair = 'GBP/CNY'
  convertForm.amount = 100
  convertResult.value = await scenarioGet(
    `/currency_conv/convert${q({ amount: 100, date: '2026-07-15', pair: 'GBP/CNY' })}`,
  )
}

onMounted(loadAll)
</script>

<style scoped>
.scenario-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 12px 0;
}
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.panel-head h3 { margin: 0; font-size: 16px; }
.panel-tip { color: var(--muted); font-size: 12.5px; margin: 6px 0 10px; }
.caliber-badge { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: #eef2ff; color: #3730a3; white-space: nowrap; }
.caliber-badge[data-caliber='legacy'] { background: #fef3c7; color: #92400e; }
.btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.btn.warn { border-color: #f0a35e; color: #b45309; background: #fff7ed; }
.panel-message { font-size: 13px; padding: 8px 10px; border-radius: 6px; background: #ecfdf3; color: #027a48; margin: 8px 0; }
.panel-message[data-ok='false'] { background: #fef3f2; color: #b42318; }
.block-title { font-size: 14px; margin: 14px 0 8px; }
.version-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; }
.version-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: #fcfcfd; }
.version-card[data-effective='true'] { border-color: #12b76a; box-shadow: 0 0 0 1px #12b76a33; }
.version-card header { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 13px; }
.status-tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #eef2f6; color: #475467; }
.status-tag[data-status='已生效'] { background: #d1fadf; color: #027a48; }
.status-tag[data-status='待生效'] { background: #fef0c7; color: #b54708; }
.status-tag[data-status='interrupted'] { background: #fee4e2; color: #b42318; }
.rate-line { display: flex; align-items: baseline; gap: 10px; margin: 8px 0 4px; }
.rate-value { font-size: 26px; font-weight: 700; color: #1d2939; }
.rate-pair { font-size: 12px; color: var(--muted); }
.source-line { font-size: 12.5px; margin: 4px 0; }
.key-line { font-size: 11.5px; color: var(--muted); word-break: break-all; margin: 4px 0; }
.warn-line { font-size: 12px; color: #b54708; }
.ok-line { font-size: 13px; color: #027a48; }
.receipt-box, .reconcile-box, .convert-box { border-top: 1px dashed var(--border); margin-top: 12px; padding-top: 4px; }
.receipt-counts { font-size: 12.5px; color: #475467; }
.review-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.review-item { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.review-item[data-verified='true'] { background: #f6fef9; }
.review-meta { display: flex; flex-direction: column; gap: 2px; font-size: 12.5px; }
.review-meta code { color: #344054; }
.verified-line { color: #027a48; font-style: normal; }
.review-form { display: flex; gap: 8px; margin-top: 6px; }
.source-input { flex: 1; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 13px; }
.json-pre { background: #1e293b; color: #e2e8f0; border-radius: 6px; padding: 10px; font-size: 12px; overflow-x: auto; }
.convert-box label { font-size: 12px; color: var(--muted); margin-right: 10px; }
.convert-box input { border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 13px; width: 150px; margin-left: 6px; }
.convert-box [data-testid='convert-pair'] { width: 110px; }
</style>
