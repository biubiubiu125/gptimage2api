<template>
  <div class="space-y-4">
    <FormSection title="基础配置">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FormField label="后台维护间隔">
          <template #label-extra>
            <HelpTip text="单位分钟，控制待核验账号、限流账号和临期 AT 的后台处理频率。" />
          </template>
          <SettingsNumberInput :field="refreshAccountIntervalField" />
        </FormField>

        <FormField label="图片访问地址">
          <template #label-extra>
            <HelpTip text="用于生成图片结果的访问前缀地址。" />
          </template>
          <Input
            v-model.trim="settings.base_url"
            block
            :disabled="fieldReadOnly('base_url')"
            placeholder="https://example.com"
          />
        </FormField>

        <FormField label="图片自动清理">
          <template #label-extra>
            <HelpTip text="自动删除多少小时前的本地图片。" />
          </template>
          <SettingsNumberInput :field="imageRetentionHoursField" />
        </FormField>

        <FormField label="日志自动清理">
          <template #label-extra>
            <HelpTip text="自动删除多少小时前的控制台调用日志。" />
          </template>
          <SettingsNumberInput :field="logRetentionHoursField" />
        </FormField>

        <FormField label="单账号图片并发">
          <template #label-extra>
            <HelpTip text="限制每个账号同时处理的图片请求数量。默认 1，可设置为 1–3。" />
          </template>
          <SettingsNumberInput :field="imageAccountConcurrencyField" />
        </FormField>

        <FormField label="账号批量任务并发">
          <template #label-extra>
            <HelpTip text="控制账号批量任务的最大并发数。刷新 AT、同步账号与额度、导入核验和后台复查按账号占用并发；启用、禁用、重置、删除和批量保存按整个批次占用一个并发。默认 30，可设置为 1–100；图片生成并发单独设置。" />
          </template>
          <SettingsNumberInput :field="accountProcessingConcurrencyField" />
        </FormField>
      </div>
    </FormSection>

    <FormSection title="请求与任务">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FormField label="控制台请求超时">
          <template #label-extra>
            <HelpTip text="单位秒，控制日志、图片、账号和设置等控制台请求等待后端响应的最长时间。" />
          </template>
          <SettingsNumberInput :field="consoleRequestTimeoutField" />
        </FormField>

      </div>
    </FormSection>

    <FormSection title="图片生成">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FormField label="图片流等待上限">
          <template #label-extra>
            <HelpTip text="单位秒，限制 ChatGPT 生图 SSE 流的等待时间。" />
          </template>
          <SettingsNumberInput :field="imageStreamTimeoutField" />
        </FormField>

        <FormField label="图片结果等待上限">
          <template #label-extra>
            <HelpTip text="单位秒，SSE 流结束后继续等待上游图片结果的最长时间。" />
          </template>
          <SettingsNumberInput :field="imagePollTimeoutField" />
        </FormField>

        <FormField label="图片首次轮询等待">
          <template #label-extra>
            <HelpTip text="单位秒，开始查询图片结果前先等待的时间。" />
          </template>
          <SettingsNumberInput :field="imagePollInitialWaitField" />
        </FormField>

        <FormField label="图片轮询间隔">
          <template #label-extra>
            <HelpTip text="单位秒，两次图片结果查询之间的等待时间。" />
          </template>
          <SettingsNumberInput :field="imagePollIntervalField" />
        </FormField>
      </div>
    </FormSection>

    <FormSection title="Cloudflare 手动 Cookie">
      <p class="text-xs leading-5 text-muted-foreground">
        注册和生图遇到 Cloudflare 拦截时，在这里填写 Chrome146 的 cf_clearance。不要使用 FlareSolverr。
      </p>
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="settings.proxy_runtime.clearance.enabled"
              :disabled="fieldReadOnly('proxy_runtime.clearance.enabled')"
            >启用手动 Cookie</Checkbox>
          </div>
        </div>
      </div>
      <FormField label="模式">
        <div class="w-full">
          <GroupedSelectMenu
            v-model="settings.proxy_runtime.clearance.mode"
            :options="clearanceModeOptions"
            :disabled="fieldReadOnly('proxy_runtime.clearance.mode')"
            selected-indicator="none"
            aria-label="Cloudflare Cookie 模式"
            block
          />
        </div>
      </FormField>
      <FormField label="cf_clearance">
        <Input
          v-model.trim="settings.proxy_runtime.clearance.cf_clearance"
          block
          :disabled="fieldReadOnly('proxy_runtime.clearance.cf_clearance')"
          placeholder="从 Chrome146 复制 cf_clearance"
        />
      </FormField>
      <FormField label="完整 Cookie">
        <textarea
          v-model="settings.proxy_runtime.clearance.cf_cookies"
          rows="3"
          class="ui-textarea-sm font-mono w-full"
          :disabled="fieldReadOnly('proxy_runtime.clearance.cf_cookies')"
          placeholder="可选，完整 Cookie 头"
        ></textarea>
      </FormField>
      <FormField label="User-Agent">
        <Input
          v-model.trim="settings.proxy_runtime.clearance.user_agent"
          block
          :disabled="fieldReadOnly('proxy_runtime.clearance.user_agent')"
        />
      </FormField>
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, FormField, FormSection, GroupedSelectMenu, HelpTip, Input } from 'nanocat-ui'
import type { Settings } from '@/types/api'
import SettingsNumberInput from '@/views/settings/SettingsNumberInput.vue'
import {
  settingsFieldOptions,
  settingsFieldReadOnly,
  type SettingsFields,
} from '@/views/settings/settingsView'
import type { NumberSettingField } from '@/views/settings/useNumberSettingField'

const props = defineProps<{
  settings: Settings
  fields: SettingsFields
  refreshAccountIntervalField: NumberSettingField
  imageRetentionHoursField: NumberSettingField
  logRetentionHoursField: NumberSettingField
  consoleRequestTimeoutField: NumberSettingField
  imagePollTimeoutField: NumberSettingField
  imageStreamTimeoutField: NumberSettingField
  imagePollInitialWaitField: NumberSettingField
  imagePollIntervalField: NumberSettingField
  imageAccountConcurrencyField: NumberSettingField
  accountProcessingConcurrencyField: NumberSettingField
}>()

const fieldReadOnly = (path: string) => settingsFieldReadOnly(props.fields, path)
const clearanceModeOptions = computed(() => (
  settingsFieldOptions(
    props.fields,
    'proxy_runtime.clearance.mode',
    props.settings.proxy_runtime.clearance.mode,
  )
))
</script>
