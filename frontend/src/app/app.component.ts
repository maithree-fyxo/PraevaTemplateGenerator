import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService, Preview, ConfigStatus } from './api.service';

interface TemplateOption {
  id: string;
  name: string;
  description: string;
  available: boolean;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './app.component.html',
  styleUrl: './app.component.scss',
})
export class AppComponent implements OnInit {
  // ---- templates (add more here as they are built) ----
  templates: TemplateOption[] = [
    {
      id: 'search-update',
      name: 'Praeva Search Update',
      description:
        'Candidate profiles, career histories and pipeline / target / discounted tables.',
      available: true,
    },
  ];
  selectedTemplateId = 'search-update';
  get selectedTemplate(): TemplateOption {
    return (
      this.templates.find((t) => t.id === this.selectedTemplateId) ??
      this.templates[0]
    );
  }

  // ---- source tabs ----
  source: 'ezekia' | 'excel' = 'ezekia';

  // ---- ezekia path ----
  url = '';
  preview: Preview | null = null;
  err = '';
  generating = false;
  previewing = false;
  diagOut = '';
  diagOpen = false;
  diagRunning = false;

  // ---- excel path ----
  file: File | null = null;
  fileName = '';
  xlsxPreview: Preview | null = null;
  xlsxErr = '';
  xlsxGenerating = false;
  xlsxPreviewing = false;

  // ---- config ----
  cfg: ConfigStatus | null = null;
  settingsOpen = false;
  tokenInput = '';
  cfgMsg = '';
  cfgErr = '';
  cfgBusy = false;

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.refreshConfig();
  }

  async refreshConfig() {
    try {
      this.cfg = await this.api.getConfig();
    } catch {
      /* ignore */
    }
  }

  // ---------- Ezekia ----------
  async runPreview() {
    this.err = '';
    this.preview = null;
    this.previewing = true;
    try {
      this.preview = await this.api.previewUrl(this.url.trim());
    } catch (e: any) {
      this.err = e?.error?.detail || 'Preview failed';
    } finally {
      this.previewing = false;
    }
  }

  async generate() {
    this.err = '';
    this.generating = true;
    try {
      await this.api.downloadPptx(
        '/api/generate',
        JSON.stringify({ url: this.url.trim() }),
        false
      );
    } catch (e: any) {
      this.err = e?.message || 'Generation failed';
    } finally {
      this.generating = false;
    }
  }

  async runDiagnose() {
    this.diagOpen = true;
    this.diagRunning = true;
    this.diagOut = 'Running diagnostics…';
    try {
      const d = await this.api.diagnose(this.url.trim());
      this.diagOut = JSON.stringify(d, null, 2);
    } catch {
      this.diagOut = 'Could not reach the server.';
    } finally {
      this.diagRunning = false;
    }
  }

  // ---------- Excel ----------
  onFile(evt: Event) {
    const input = evt.target as HTMLInputElement;
    this.file = input.files && input.files[0] ? input.files[0] : null;
    this.fileName = this.file ? this.file.name : '';
    this.xlsxPreview = null;
    this.xlsxErr = '';
  }

  async runXlsxPreview() {
    this.xlsxErr = '';
    this.xlsxPreview = null;
    if (!this.file) {
      this.xlsxErr = 'Choose an .xlsx file first.';
      return;
    }
    this.xlsxPreviewing = true;
    try {
      this.xlsxPreview = await this.api.previewExcel(this.file);
    } catch (e: any) {
      this.xlsxErr = e?.error?.detail || 'Preview failed';
    } finally {
      this.xlsxPreviewing = false;
    }
  }

  async xlsxGenerate() {
    this.xlsxErr = '';
    if (!this.file) {
      this.xlsxErr = 'Choose an .xlsx file first.';
      return;
    }
    this.xlsxGenerating = true;
    try {
      const fd = new FormData();
      fd.append('file', this.file);
      await this.api.downloadPptx('/api/generate-from-excel', fd, true);
    } catch (e: any) {
      this.xlsxErr = e?.message || 'Generation failed';
    } finally {
      this.xlsxGenerating = false;
    }
  }

  // ---------- config ----------
  async saveToken() {
    const v = this.tokenInput.trim();
    this.cfgMsg = '';
    this.cfgErr = '';
    if (!v) {
      this.cfgErr = 'Enter a key first.';
      return;
    }
    this.cfgBusy = true;
    try {
      this.cfg = await this.api.saveToken(v);
      this.tokenInput = '';
      this.cfgMsg = "Key saved on the server. It won't be shown again.";
    } catch (e: any) {
      this.cfgErr = e?.error?.detail || 'Save failed';
    } finally {
      this.cfgBusy = false;
    }
  }

  async testToken() {
    this.cfgMsg = 'Testing…';
    this.cfgErr = '';
    try {
      const d = await this.api.testToken();
      if (d.ok) this.cfgMsg = '✓ ' + d.detail;
      else {
        this.cfgErr = (d.status ? '[' + d.status + '] ' : '') + d.detail;
        this.cfgMsg = '';
      }
    } catch {
      this.cfgErr = 'Could not reach the server.';
      this.cfgMsg = '';
    }
  }

  async clearToken() {
    this.cfgBusy = true;
    this.cfgMsg = '';
    this.cfgErr = '';
    try {
      this.cfg = await this.api.clearToken();
      this.tokenInput = '';
      this.cfgMsg = 'Key cleared — the app is back in demo mode.';
    } catch {
      this.cfgErr = 'Could not reach the server.';
    } finally {
      this.cfgBusy = false;
    }
  }

  chips(p: Preview) {
    return [
      { k: 'Engaged · profiles', v: p.counts.engaged_profiles },
      { k: 'Pipeline · table', v: p.counts.pipeline_table },
      { k: 'Discounted · profiles', v: p.counts.discounted_profiles },
      { k: 'Discounted · table', v: p.counts.discounted_table },
      { k: 'Target', v: p.counts.target },
    ];
  }
}
