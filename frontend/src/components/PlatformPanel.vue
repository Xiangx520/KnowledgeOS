<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
type Limits = Record<string, number>
type Job = { id: string; name: string; status: string; progress: number | null; stage?: string; processed_pages: number; total_pages: number; attempts: number; error?: string; lease_expires_at?: string }
const people = ref<{id: string; email: string; name: string}[]>([])
const emit = defineEmits<{changed: []}>()
let pollTimer: ReturnType<typeof setInterval> | undefined
const selectedUser = ref(''), limits = ref<Limits>({}), overridden = ref(false)
const kind = ref('ingestion'), status = ref(''), jobs = ref<Job[]>([])
const busy = ref(false), error = ref(''), notice = ref('')
const confirmAction = ref<{job: Job; action: 'cancel'|'retry'} | null>(null)
const labels: Record<string, string> = {user_storage_mb:'存储上限（MB）',max_pending_jobs:'待处理任务上限',max_active_chats:'账户同时问答上限',global_active_chats:'整个服务同时问答上限',user_daily_model_requests:'每日模型请求预算'}
const states: Record<string, string> = {queued:'排队中',processing:'处理中',running:'运行中',completed:'已完成',needs_review:'待人工审核',failed:'失败',cancelled:'已取消'}
async function run(action: () => Promise<unknown>) { busy.value=true; error.value=''; try { await action() } catch(e) {error.value=e instanceof Error?e.message:'操作失败'} finally {busy.value=false} }
function formatDate(value: string) {return new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)?value:value+'Z').toLocaleString()}
function suffix() {return selectedUser.value ? `?user_id=${encodeURIComponent(selectedUser.value)}` : ''}
async function loadLimits() {const result=await api<{limits: Limits; overridden: boolean}>('/platform/quotas'+suffix());limits.value=result.limits;overridden.value=result.overridden}
async function loadJobs() {jobs.value=await api(`/platform/jobs?kind=${kind.value}${status.value?'&status='+status.value:''}`)}
async function save() {await api('/platform/quotas'+suffix(),{method:'PUT',body:JSON.stringify(limits.value)});notice.value='配额已保存，新请求立即生效；正在执行的任务保持当前配置。';await loadLimits();emit('changed')}
async function reset() {await api('/platform/quotas'+suffix(),{method:'DELETE'});notice.value='已恢复继承全局配额';await loadLimits();emit('changed')}
async function control() {if(!confirmAction.value)return;const {job,action}=confirmAction.value;await api(`/platform/jobs/${kind.value}/${job.id}/${action}`,{method:'POST'});confirmAction.value=null;notice.value=action==='cancel'?'已取消任务，已发布文档版本仍可使用。':'已提交重试。';await loadJobs()}
onMounted(()=>{void run(async()=>{people.value=await api('/platform/users');await loadLimits();await loadJobs()});pollTimer=setInterval(()=>{if(!busy.value)void loadJobs().catch(()=>{})},8000)})
onUnmounted(()=>clearInterval(pollTimer))
</script>
<template>
  <section class="panel platform-panel">
    <div class="section-heading"><h2>平台管理</h2><span class="outline-chip">最高管理员</span></div>
    <p>调整全局默认配额或指定账号配额。平台管理权限由部署配置指定，组织管理员无法自行提升。</p>
    <p v-if="error" role="alert" class="gov-error">{{error}}</p><p v-if="notice" role="status" class="platform-notice">{{notice}}</p>
    <label>配额作用范围<select v-model="selectedUser" :disabled="busy" @change="run(loadLimits)"><option value="">全局默认（所有未单独设置的账号）</option><option v-for="person in people" :key="person.id" :value="person.id">{{person.name}} · {{person.email}}</option></select></label>
    <form class="quota-editor" @submit.prevent="run(save)">
      <label v-for="key in Object.keys(limits)" :key="key">{{labels[key]||key}}<input v-model.number="limits[key]" type="number" required min="1" :max="key==='max_active_chats'?32:key==='global_active_chats'?128:key==='max_pending_jobs'?10000:key==='user_storage_mb'?10485760:10000000" :disabled="busy || (!!selectedUser && key==='global_active_chats')" /></label>
      <div class="platform-actions"><button type="submit" class="btn primary" :disabled="busy">保存配额</button><button v-if="selectedUser&&overridden" type="button" class="btn secondary" :disabled="busy" @click="run(reset)">恢复全局默认</button></div>
    </form>
    <p class="platform-hint">存储按知识库所有者计算；整个服务并发上限始终由全局控制。模型请求预算不是 token 计费。</p>
    <div class="section-heading"><h2>任务管理</h2><button class="btn secondary" :disabled="busy" @click="run(loadJobs)">刷新任务</button></div>
    <p class="platform-hint">显示最近50个匹配任务，每8秒更新。可按任务类型和状态筛选。</p>
    <div class="platform-actions"><label>任务类型<select v-model="kind" @change="run(loadJobs)"><option value="ingestion">文档解析与索引</option><option value="evaluation">质量评测</option><option value="cleanup">文件与向量清理</option></select></label><label>状态<select v-model="status" @change="run(loadJobs)"><option value="">全部</option><option v-for="(label,key) in states" :key="key" :value="key">{{label}}</option></select></label></div>
    <div class="table-scroll"><table class="platform-jobs"><thead><tr><th>任务</th><th>状态与进度</th><th>操作</th></tr></thead><tbody><tr v-for="job in jobs" :key="job.id"><td><strong>{{job.name}}</strong><small v-if="job.lease_expires_at">租约至 {{formatDate(job.lease_expires_at)}}</small><p v-if="job.error" class="gov-error">{{job.error}}</p></td><td>{{states[job.status]||job.status}}<span v-if="job.progress!==null"> · {{job.progress}}%</span><small>{{job.stage}}<span v-if="job.total_pages"> · {{job.processed_pages}} / {{job.total_pages}} 页</span></small></td><td><button v-if="kind!=='cleanup'&&['queued','processing','running'].includes(job.status)" class="btn secondary" :disabled="busy" @click="confirmAction={job,action:'cancel'}">取消任务</button><button v-if="['failed','needs_review','cancelled','queued','processing','running'].includes(job.status)" class="btn secondary" :disabled="busy" @click="confirmAction={job,action:'retry'}">重试</button></td></tr></tbody></table><p v-if="!jobs.length">当前没有符合条件的任务。</p></div>
    <div v-if="confirmAction" class="modal-backdrop" @click.self="confirmAction=null"><section class="modal" role="dialog" aria-modal="true" aria-label="确认任务操作"><h2>{{confirmAction.action==='cancel'?'取消任务':'重试任务'}}</h2><p>{{confirmAction.job.name}}</p><p>{{confirmAction.action==='cancel'?'停止此任务继续写入，保留原文件和已经发布的版本。上游模型调用会在返回后退出。':'重新提交任务；仍在运行的任务需要先取消。'}}</p><div class="platform-actions"><button class="btn secondary" :disabled="busy" @click="confirmAction=null">返回</button><button class="btn primary" :disabled="busy" @click="run(control)">{{busy?'正在处理…':'确认操作'}}</button></div></section></div>
  </section>
</template>
