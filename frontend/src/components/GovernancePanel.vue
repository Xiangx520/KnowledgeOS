<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import type { KnowledgeBase } from '../api'
import PlatformPanel from './PlatformPanel.vue'
function roleLabel(role: string) { return ({ admin: '管理员', editor: '编辑', reader: '只读成员' } as Record<string, string>)[role] || role }
type Org = { id: string; name: string; role: string }
type Member = { id: string; email: string; name?: string; role: string }
const props = defineProps<{ onChanged: () => Promise<void> }>()
const organizations = ref<Org[]>([]), selected = ref(''), members = ref<Member[]>([])
const name = ref(''), email = ref(''), role = ref('reader'), error = ref(''), busy = ref(false)
type Operations = { ingestion_jobs: Record<string, number>; limits: Record<string, number>; my_audit_events: number; is_platform_admin: boolean }
const events = ref<Record<string, unknown>[]>([]), operations = ref<Operations | null>(null)
const limitLabels: Record<string, string> = { user_storage_mb: '存储配额（MB）', max_pending_jobs: '待处理任务上限', max_active_chats: '同时问答上限', global_active_chats: '服务同时问答上限', user_daily_model_requests: '每日模型请求预算' }
const statusLabels: Record<string, string> = { queued: '排队中', processing: '处理中', completed: '已完成', needs_review: '待人工审核', failed: '需重试', cancelled: '已取消' }
function actionLabel(value: unknown) {
  const labels: Record<string, string> = { 'session.create': '登录工作台', 'session.revoke': '退出登录', 'session.revoke_all': '退出所有设备', 'organization.create': '创建组织', 'knowledge_base.create': '创建知识库', 'knowledge_base.delete': '删除知识库', 'document.upload': '上传文档', 'document.version.upload': '更新文档版本', 'document.reindex': '重新索引文档', 'document.source.sync': '同步文档来源', 'document.restore': '恢复文档版本', 'document.metadata': '更新来源与有效期', 'document.download': '下载原文', 'document.version.download': '下载历史版本', 'document.delete': '删除文档', 'chat.complete': '完成问答', 'evaluation.create': '提交评测', 'membership.remove': '移除组织成员', 'knowledge_base.revoke': '撤销知识库授权' }
  const action = String(value)
  if (action === 'platform.quotas.update') return '更新运行配额'
  if (action === 'platform.quotas.reset') return '恢复默认配额'
  if (action === 'platform.job.cancel') return '取消处理任务'
  if (action === 'platform.job.retry') return '重试处理任务'
  if (action.startsWith('membership.set_role.')) return '更新组织成员角色'
  if (action.startsWith('knowledge_base.grant.')) return '更新知识库授权'
  return labels[action] || action
}
const bases = ref<KnowledgeBase[]>([]), grantKb = ref(''), grantEmail = ref(''), grantRole = ref('reader')
const grants = ref<Member[]>([]), syncKb = ref(''), syncPath = ref(''), syncResult = ref('')
async function loadGrants() { grants.value = grantKb.value ? await api(`/knowledge-bases/${grantKb.value}/members`) : [] }
async function syncDocuments() {
  const result = await api<{ files: { unchanged: boolean }[] }>('/sources/sync', {
    method: 'POST', body: JSON.stringify({ knowledge_base_id: syncKb.value, relative_path: syncPath.value }) })
  syncResult.value = `已检查 ${result.files.length} 份文档，${result.files.filter(item => !item.unchanged).length} 份已提交更新。`
}
function reload() { window.location.reload() }
async function refresh() {
  organizations.value = await api<Org[]>('/organizations')
  if (!selected.value) selected.value = organizations.value[0]?.id || ''
  await load()
  operations.value = await api<Operations>('/operations')
  bases.value = await api<KnowledgeBase[]>('/knowledge-bases')
  await loadGrants()
}
async function load() {
  const org = organizations.value.find(item => item.id === selected.value)
  members.value = org?.role === 'admin' ? await api(`/organizations/${selected.value}/members`) : []
  events.value = await api(`/audit-events${org?.role === 'admin' ? `?organization_id=${selected.value}` : ''}`)
}
async function run(action: () => Promise<unknown>) {
  busy.value = true; error.value = ''
  try { await action(); await refresh(); await props.onChanged() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '操作失败' }
  finally { busy.value = false }
}
onMounted(() => run(refresh))
</script>
<template>
  <main class="page governance-page">
    <div class="page-heading"><div><span class="eyebrow">WORKSPACE GOVERNANCE</span><h1>团队与治理<span class="heading-dot">.</span></h1><p>管理组织成员、访问角色与操作记录。</p></div><button class="btn secondary" :disabled="busy" @click="run(refresh)">刷新治理状态</button></div>
    <p v-if="error" role="alert" class="gov-error">{{ error }}</p>
    <PlatformPanel v-if="operations?.is_platform_admin" @changed="run(refresh)" />
    <div class="gov-grid"><section class="panel gov-panel"><h2>组织空间</h2>
      <form @submit.prevent="run(async () => { const org = await api<Org>('/organizations', { method: 'POST', body: JSON.stringify({ name }) }); selected = org.id; name = '' })">
        <label>新组织名称<input v-model="name" required maxlength="120" placeholder="例如：产品与技术中心" /></label><button class="btn primary" :disabled="busy">创建组织</button>
      </form>
      <label>当前组织<select v-model="selected" @change="run(load)"><option value="">个人空间</option><option v-for="org in organizations" :key="org.id" :value="org.id">{{ org.name }} · {{ roleLabel(org.role) }}</option></select></label>
      <p class="gov-muted">在知识空间新建知识库时选择组织。管理员负责成员和知识库；编辑可更新文档；只读成员可以检索、问答及下载。</p>
    </section><section class="panel gov-panel"><h2>成员与角色</h2>
      <template v-if="organizations.find(item => item.id === selected)?.role === 'admin'">
        <form @submit.prevent="run(() => api(`/organizations/${selected}/members`, { method: 'PUT', body: JSON.stringify({ email, role }) }))">
          <label>已注册成员邮箱<input v-model="email" type="email" required placeholder="member@company.com" /></label>
          <label>角色<select v-model="role"><option value="reader">只读成员</option><option value="editor">编辑</option><option value="admin">管理员</option></select></label><button class="btn primary" :disabled="busy">添加或更新角色</button>
        </form>
        <div v-for="member in members" :key="member.id" class="gov-member"><div><strong>{{ member.name || member.email }}</strong><small>{{ member.email }} · {{ roleLabel(member.role) }}</small></div><button class="text-button" :disabled="busy" @click="run(() => api(`/organizations/${selected}/members/${member.id}`, { method: 'DELETE' }))">移除成员</button></div>
      </template><p v-else class="gov-muted">成员管理仅向组织管理员开放。</p>
    </section></div>
    <section class="panel gov-panel"><h2>知识库单独授权</h2><p class="gov-muted">指定成员可见的知识库只向被授权成员开放。组织管理员可管理全部团队知识库。</p>
      <label>受限知识库<select v-model="grantKb" @change="run(loadGrants)"><option value="">请选择</option><option v-for="kb in bases.filter(item => item.organization_id && item.visibility === 'restricted' && item.role === 'admin')" :key="kb.id" :value="kb.id">{{ kb.name }}</option></select></label>
      <form v-if="grantKb" @submit.prevent="run(() => api(`/knowledge-bases/${grantKb}/members`, { method: 'PUT', body: JSON.stringify({ email: grantEmail, role: grantRole }) }))"><label>组织成员邮箱<input v-model="grantEmail" required type="email" /></label><label>知识库角色<select v-model="grantRole"><option value="reader">只读</option><option value="editor">编辑</option><option value="admin">管理员</option></select></label><button class="btn primary" :disabled="busy">保存授权</button></form>
      <div v-for="grant in grants" :key="grant.id" class="gov-member"><span>{{ grant.email }} · {{ roleLabel(grant.role) }}</span><button class="text-button" :disabled="busy" @click="run(() => api(`/knowledge-bases/${grantKb}/members/${grant.id}`, { method: 'DELETE' }))">撤销授权</button></div>
    </section><section class="panel gov-panel"><h2>同步文档源</h2><p class="gov-muted">从管理员配置的文档源目录增量导入，内容改变时生成新版本。源文件移除后，已有版本需显式过期或删除。</p><form @submit.prevent="run(syncDocuments)"><label>目标知识库<select v-model="syncKb" required><option value="">请选择</option><option v-for="kb in bases.filter(item => item.role !== 'reader')" :key="kb.id" :value="kb.id">{{ kb.name }}</option></select></label><label>源内相对路径<input v-model="syncPath" required placeholder="例如：policies 或 manuals/product.pdf" /></label><button class="btn primary" :disabled="busy">执行同步</button></form><pre v-if="syncResult">{{ syncResult }}</pre></section><section class="panel gov-panel"><h2>运行配额与任务</h2><div v-if="operations" class="gov-limits"><div v-for="(value, key) in operations.limits" :key="key"><small>{{ limitLabels[key] || key }}</small><strong>{{ value }}</strong></div></div><p v-if="operations" class="gov-muted"><span v-for="(value, key) in operations.ingestion_jobs" :key="key">{{ statusLabels[key] || key }} {{ value }}　</span>· 已记录 {{ operations.my_audit_events }} 次操作</p><button class="btn secondary" @click="run(() => api('/auth/sessions', { method: 'DELETE' }).then(reload))">退出所有登录设备</button></section>
    <section class="panel gov-panel"><h2>审计记录</h2><p class="gov-muted">记录操作人与资源标识，不保存密码、令牌或文档正文。</p><div class="gov-events"><table><thead><tr><th>时间</th><th>操作</th><th>资源</th><th>操作人</th></tr></thead><tbody><tr v-for="event in events" :key="String(event.id)"><td>{{ new Date(String(event.created_at) + 'Z').toLocaleString() }}</td><td :title="String(event.action)">{{ actionLabel(event.action) }}</td><td>{{ event.resource_id }}</td><td>{{ event.actor_id }}</td></tr></tbody></table></div></section>
  </main>
</template>
<style scoped>
.gov-limits{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin:20px 0}.gov-limits div{background:#f5f8f6;border:1px solid #e3ebe6;border-radius:10px;padding:18px}.gov-limits small{display:block;color:#718078;font-size:14px}.gov-limits strong{display:block;font-size:26px;margin-top:10px}.gov-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.gov-panel{padding:24px;margin-bottom:20px}.gov-panel h2{margin-bottom:18px;font-size:18px}.gov-panel form{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin-bottom:20px}.gov-panel label{display:flex;flex-direction:column;gap:8px;font-size:14px;flex:1}.gov-panel input,.gov-panel select{border:1px solid #dce5df;border-radius:8px;padding:10px;background:#fff;min-width:0}.gov-panel pre{font-size:14px;white-space:pre-wrap;line-height:1.7}.gov-muted{color:#6b7c73;font-size:14px;line-height:1.8;margin:12px 0}.gov-member{display:flex;align-items:center;justify-content:space-between;padding:14px 0;border-top:1px solid #edf1ee}.gov-member small{display:block;color:#7b8981;margin-top:4px}.gov-events{overflow:auto}.gov-events table{font-size:14px;width:100%;border-collapse:collapse}.gov-events td,.gov-events th{text-align:left;padding:12px;border-bottom:1px solid #edf1ee;white-space:nowrap}.gov-error{color:#b34747;margin-bottom:20px}@media(max-width:900px){.gov-grid{grid-template-columns:1fr}}
</style>
