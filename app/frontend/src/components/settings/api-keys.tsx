import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useI18n } from '@/i18n/use-i18n';
import { apiKeysService } from '@/services/api-keys-api';
import { Eye, EyeOff, Key, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';

interface ApiKey {
  key: string;
  label: string;
  description: string;
  url: string;
  placeholder: string;
}

const FINANCIAL_API_KEYS: ApiKey[] = [
  {
    key: 'FINANCIAL_DATASETS_API_KEY',
    label: 'Financial Datasets API',
    description: 'For getting financial data to power the hedge fund',
    url: 'https://financialdatasets.ai/',
    placeholder: 'your-financial-datasets-api-key'
  }
];

const LLM_API_KEYS: ApiKey[] = [
  {
    key: 'ANTHROPIC_API_KEY',
    label: 'Anthropic API',
    description: 'For Claude models (claude-4-sonnet, claude-4.1-opus, etc.)',
    url: 'https://anthropic.com/',
    placeholder: 'your-anthropic-api-key'
  },
  {
    key: 'DEEPSEEK_API_KEY',
    label: 'DeepSeek API',
    description: 'For DeepSeek models (deepseek-chat, deepseek-reasoner, etc.)',
    url: 'https://deepseek.com/',
    placeholder: 'your-deepseek-api-key'
  },
  {
    key: 'GROQ_API_KEY',
    label: 'Groq API',
    description: 'For Groq-hosted models (deepseek, llama3, etc.)',
    url: 'https://groq.com/',
    placeholder: 'your-groq-api-key'
  },
  {
    key: 'GOOGLE_API_KEY',
    label: 'Google API',
    description: 'For Gemini models (gemini-2.5-flash, gemini-2.5-pro)',
    url: 'https://ai.dev/',
    placeholder: 'your-google-api-key'
  },
  {
    key: 'OPENAI_API_KEY',
    label: 'OpenAI API',
    description: 'For OpenAI models (gpt-4o, gpt-4o-mini, etc.)',
    url: 'https://platform.openai.com/',
    placeholder: 'your-openai-api-key'
  },
  {
    key: 'OPENROUTER_API_KEY',
    label: 'OpenRouter API',
    description: 'For OpenRouter models (gpt-4o, gpt-4o-mini, etc.)',
    url: 'https://openrouter.ai/',
    placeholder: 'your-openrouter-api-key'
  },
  {
    key: 'GIGACHAT_API_KEY',
    label: 'GigaChat API',
    description: 'For GigaChat models (GigaChat-2-Max, etc.)',
    url: 'https://github.com/ai-forever/gigachat',
    placeholder: 'your-gigachat-api-key'
  }
];

interface KeyStatus {
  isSet: boolean;
  maskedTail: string;
}

export function ApiKeysSettings() {
  // Per-provider status (never the raw secret); a transient draft for the replace flow.
  const [keyStatus, setKeyStatus] = useState<Record<string, KeyStatus>>({});
  const [draftKey, setDraftKey] = useState<Record<string, string>>({});
  const [isReplacing, setIsReplacing] = useState<Record<string, boolean>>({});
  const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { t } = useI18n();

  // Load API keys from backend on component mount (run once).
  useEffect(() => {
    loadApiKeys();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadApiKeys = async () => {
    try {
      setLoading(true);
      setError(null);
      // One list call — the raw secret is never fetched into the browser.
      const summaries = await apiKeysService.getAllApiKeys();
      const status: Record<string, KeyStatus> = {};
      for (const summary of summaries) {
        status[summary.provider] = {
          isSet: summary.is_set,
          maskedTail: summary.masked_tail ?? '',
        };
      }
      setKeyStatus(status);
    } catch (err) {
      console.error('Failed to load API keys:', err);
      setError(t('apiKeys.loadError'));
    } finally {
      setLoading(false);
    }
  };

  const startReplace = (key: string) => {
    setIsReplacing(prev => ({ ...prev, [key]: true }));
    setDraftKey(prev => ({ ...prev, [key]: '' }));
  };

  const cancelReplace = (key: string) => {
    setIsReplacing(prev => ({ ...prev, [key]: false }));
    setDraftKey(prev => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setVisibleKeys(prev => ({ ...prev, [key]: false }));
  };

  // Editing only updates the transient draft — NO network call, so an empty input
  // can never auto-save or delete a stored key.
  const handleDraftChange = (key: string, value: string) => {
    setDraftKey(prev => ({ ...prev, [key]: value }));
  };

  const saveKey = async (key: string) => {
    const value = (draftKey[key] ?? '').trim();
    if (!value) return; // the only write path; an empty draft never writes
    try {
      await apiKeysService.createOrUpdateApiKey({ provider: key, key_value: value, is_active: true });
      setIsReplacing(prev => ({ ...prev, [key]: false }));
      setDraftKey(prev => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      setVisibleKeys(prev => ({ ...prev, [key]: false }));
      await loadApiKeys();
    } catch (err) {
      console.error(`Failed to save API key ${key}:`, err);
      setError(t('apiKeys.saveError', { key }));
    }
  };

  const toggleKeyVisibility = (key: string) => {
    setVisibleKeys(prev => ({
      ...prev,
      [key]: !prev[key]
    }));
  };

  const clearKey = async (key: string) => {
    try {
      await apiKeysService.deleteApiKey(key);
      // Re-sync from the backend (authoritative) rather than an optimistic local delete, so the
      // displayed key-presence state always reflects the server — mirrors saveKey. Key-presence is
      // security-sensitive: it must never be shown from an unconfirmed optimistic mutation.
      await loadApiKeys();
    } catch (err) {
      console.error(`Failed to delete API key ${key}:`, err);
      setError(t('apiKeys.deleteError', { key }));
    }
  };

  const renderApiKeySection = (title: string, description: string, keys: ApiKey[], icon: React.ReactNode) => (
    <Card className="bg-panel border-gray-700 dark:border-gray-700">
      <CardHeader>
        <CardTitle className="text-lg font-medium text-primary flex items-center gap-2">
          {icon}
          {title}
        </CardTitle>
        <p className="text-sm text-muted-foreground">{description}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {keys.map((apiKey) => {
          const status = keyStatus[apiKey.key];
          const isSet = status?.isSet ?? false;
          const replacing = isReplacing[apiKey.key] ?? false;
          return (
          <div key={apiKey.key} className="space-y-2">
            <button
              className="text-sm font-medium text-primary hover:text-blue-500 cursor-pointer transition-colors text-left"
              onClick={() => window.open(apiKey.url, '_blank')}
            >
              {apiKey.label}
            </button>
            {isSet && !replacing ? (
              // Configured: a masked read-only view — the secret is never in the DOM.
              <div className="flex items-center gap-2">
                <div className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm font-mono text-muted-foreground">
                  {'••••••••'}{status?.maskedTail ? ` ${status.maskedTail}` : ''}
                </div>
                <Button variant="outline" size="sm" onClick={() => startReplace(apiKey.key)}>
                  {t('apiKeys.replace')}
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={t('apiKeys.deleteKey', { label: apiKey.label })}
                  className="h-9 w-9 hover:bg-red-500/10 hover:text-red-500"
                  onClick={() => clearKey(apiKey.key)}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ) : (
              // Not set, or replacing: edit a transient draft; Save is the only write.
              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Input
                    type={visibleKeys[apiKey.key] ? 'text' : 'password'}
                    placeholder={t('apiKeys.enterKey')}
                    value={draftKey[apiKey.key] || ''}
                    onChange={(e) => handleDraftChange(apiKey.key, e.target.value)}
                    className="pr-10"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="absolute right-1 top-1/2 -translate-y-1/2 h-7 w-7"
                    onClick={() => toggleKeyVisibility(apiKey.key)}
                  >
                    {visibleKeys[apiKey.key] ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                  </Button>
                </div>
                <Button
                  variant="default"
                  size="sm"
                  disabled={!(draftKey[apiKey.key] || '').trim()}
                  onClick={() => saveKey(apiKey.key)}
                >
                  {t('apiKeys.save')}
                </Button>
                {isSet && (
                  <Button variant="ghost" size="sm" onClick={() => cancelReplace(apiKey.key)}>
                    {t('apiKeys.cancel')}
                  </Button>
                )}
              </div>
            )}
          </div>
          );
        })}
      </CardContent>
    </Card>
  );

  if (loading) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-xl font-semibold text-primary mb-2">{t('apiKeys.loadingTitle')}</h2>
          <p className="text-sm text-muted-foreground">
            {t('apiKeys.loading')}
          </p>
        </div>
        <Card className="bg-panel border-gray-700 dark:border-gray-700">
          <CardContent className="p-6">
            <div className="text-sm text-muted-foreground">
              {t('apiKeys.loadingWait')}
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-primary mb-2">{t('settings.apiKeys')}</h2>
        <p className="text-sm text-muted-foreground">
          {t('apiKeys.description')}
        </p>
      </div>

      {/* Error Message */}
      {error && (
        <Card className="bg-red-500/5 border-red-500/20">
          <CardContent className="p-4">
            <div className="flex items-start gap-3">
              <Key className="h-5 w-5 text-red-500 mt-0.5 flex-shrink-0" />
              <div className="space-y-1">
                <h4 className="text-sm font-medium text-red-500">{t('apiKeys.errorTitle')}</h4>
                <p className="text-xs text-muted-foreground">{error}</p>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setError(null);
                    loadApiKeys();
                  }}
                  className="text-xs mt-2 p-0 h-auto text-red-500 hover:text-red-400"
                >
                  {t('apiKeys.tryAgain')}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Financial Data API Keys */}
      {renderApiKeySection(
        t('apiKeys.financialData'),
        t('apiKeys.financialDataDescription'),
        FINANCIAL_API_KEYS,
        <Key className="h-4 w-4" />
      )}

      {/* LLM API Keys */}
      {renderApiKeySection(
        t('apiKeys.languageModels'),
        t('apiKeys.languageModelsDescription'),
        LLM_API_KEYS,
        <Key className="h-4 w-4" />
      )}

      {/* Security Note */}
      <Card className="bg-amber-500/5 border-amber-500/20">
        <CardContent className="p-4">
          <div className="flex items-start gap-3">
            <Key className="h-5 w-5 text-amber-500 mt-0.5 flex-shrink-0" />
            <div className="space-y-1">
              <h4 className="text-sm font-medium text-amber-500">{t('apiKeys.securityNoteTitle')}</h4>
              <p className="text-xs text-muted-foreground">
                {t('apiKeys.securityNote')}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
