import React from 'react';
import {createRoot} from 'react-dom/client';
import {I18nextProvider} from 'react-i18next';
import i18n from './i18n';
import {App} from './App';
import './styles.css';

// Single entry. The production bundle (one app.js) mounts into #app, which the
// hand-written pangpang/web/index.html provides. The dev-server index.html uses #root;
// support both so dev and prod share one entry.
const mount = document.getElementById('app') || document.getElementById('root');
createRoot(mount!).render(<React.StrictMode><I18nextProvider i18n={i18n}><App /></I18nextProvider></React.StrictMode>);
