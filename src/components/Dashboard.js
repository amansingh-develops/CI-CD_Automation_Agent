import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Github, GitBranch, Activity, Clock, CheckCircle, XCircle, AlertTriangle, Sparkles, Zap, Shield } from 'lucide-react';
import { Progress } from '@/components/ui/progress';
import axios from 'axios';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const themes = {
  idle: {
    primary: '#FFA500',
    glow: 'rgba(255, 165, 0, 0.4)',
    bg: 'bg-theme-idle',
    radial: 'radial-gradient(circle at 50% 20%, rgba(255, 165, 0, 0.08) 0%, transparent 50%)'
  },
  analyzing: {
    primary: '#FFFFFF',
    glow: 'rgba(255, 255, 255, 0.4)',
    bg: 'bg-theme-analyzing',
    radial: 'radial-gradient(circle at 50% 20%, rgba(255, 255, 255, 0.08) 0%, transparent 50%)'
  },
  error: {
    primary: '#FF4444',
    glow: 'rgba(255, 68, 68, 0.4)',
    bg: 'bg-theme-error',
    radial: 'radial-gradient(circle at 50% 20%, rgba(255, 68, 68, 0.08) 0%, transparent 50%)'
  },
  success: {
    primary: '#00FF88',
    glow: 'rgba(0, 255, 136, 0.4)',
    bg: 'bg-theme-success',
    radial: 'radial-gradient(circle at 50% 20%, rgba(0, 255, 136, 0.08) 0%, transparent 50%)'
  }
};

const BubbleEffect = ({ isActive, themeColor, speed = 1 }) => {
  const [bubbles, setBubbles] = useState([]);

  useEffect(() => {
    if (!isActive) {
      setBubbles([]);
      return;
    }

    const interval = setInterval(() => {
      const newBubble = {
        id: Math.random(),
        left: Math.random() * 100,
        size: 20 + Math.random() * 40,
        delay: Math.random() * 2 / speed,
        duration: (3 + Math.random() * 2) / speed
      };
      setBubbles(prev => [...prev.slice(-15), newBubble]);
    }, 300 / speed);

    return () => clearInterval(interval);
  }, [isActive, speed]);

  if (!isActive) return null;

  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none">
      {bubbles.map(bubble => (
        <motion.div
          key={bubble.id}
          className="bubble"
          initial={{
            bottom: -50,
            left: `${bubble.left}%`,
            scale: 0,
            opacity: 0
          }}
          animate={{
            bottom: '120%',
            scale: 1,
            opacity: [0, 0.8, 0]
          }}
          transition={{
            duration: bubble.duration,
            delay: bubble.delay,
            ease: 'easeOut'
          }}
          style={{
            width: bubble.size,
            height: bubble.size,
            background: `radial-gradient(circle at 30% 30%, ${themeColor}66, ${themeColor}22)`,
            borderColor: themeColor
          }}
        />
      ))}
    </div>
  );
};

export const Dashboard = () => {
  const [repoUrl, setRepoUrl] = useState('');
  const [teamName, setTeamName] = useState('');
  const [leaderName, setLeaderName] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
  const [showResults, setShowResults] = useState(false);
  const [uiState, setUiState] = useState('idle');
  const [apiData, setApiData] = useState(null);
  const [bubbleSpeed, setBubbleSpeed] = useState(1);
  const [error, setError] = useState(null);

  const currentTheme = themes[uiState];

  const handleAnalyze = async () => {
    setError(null);
    setAnalyzing(true);
    setUiState('analyzing');
    setBubbleSpeed(1);

    try {
      // Simulate error detection phase
      setTimeout(() => {
        setUiState('error');
        setBubbleSpeed(2);
      }, 2000);

      // Call backend API
      const response = await axios.post(`${API}/analyze-repository`, {
        repo_url: repoUrl,
        team_name: teamName,
        leader_name: leaderName
      });

      // Transition to success
      setUiState('success');
      setBubbleSpeed(1.5);
      setApiData(response.data);

      setTimeout(() => {
        setAnalyzing(false);
        setShowResults(true);
      }, 1000);

    } catch (err) {
      console.error('Analysis error:', err);
      setError(err.response?.data?.detail || 'Failed to analyze repository');
      setUiState('error');
      setAnalyzing(false);
    }
  };

  const branchName = apiData?.branch_name ||
    (teamName && leaderName
      ? `${teamName.toUpperCase().replace(/\s+/g, '_')}_${leaderName.toUpperCase().replace(/\s+/g, '_')}_AI_FIX`
      : 'TEAM_NAME_LEADER_NAME_AI_FIX');

  const hasFailed = apiData?.timeline?.some(t => t.status === 'FAILED') || false;
  const cicdStatus = hasFailed ? 'FAILED' : 'PASSED';

  return (
    <div className={`min-h-screen ${currentTheme.bg} theme-transition relative overflow-hidden`}>
      {/* Background effects */}
      <div
        className="absolute inset-0 pointer-events-none theme-transition"
        style={{ background: currentTheme.radial }}
      />
      <div className="absolute inset-0 bg-[url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI1IiBoZWlnaHQ9IjUiPgo8cmVjdCB3aWR0aD0iNSIgaGVpZ2h0PSI1IiBmaWxsPSIjMDAwMDAwIj48L3JlY3Q+CjxwYXRoIGQ9Ik0wIDVMNSAwWk02IDRMNCA2Wk0tMSAxTDEgLTFaIiBzdHJva2U9IiMxMTExMTEiIHN0cm9rZS13aWR0aD0iMSI+PC9wYXRoPgo8L3N2Zz4=')] opacity-20" />

      <div className="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 lg:py-12">
        {/* Header */}
        <motion.div
          initial={{ opacity: 0, y: -20 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center mb-12"
        >
          <h1
            className="text-4xl sm:text-5xl lg:text-6xl font-bold font-rajdhani tracking-tight mb-3 theme-transition"
            style={{ color: currentTheme.primary }}
          >
            AUTONOMOUS DEVOPS AI AGENT
          </h1>
          <p className="text-gray-400 text-base sm:text-lg font-inter">Intelligent code analysis and automated fixes powered by AI</p>
        </motion.div>

        {/* Central Octocat with Bubbles */}
        <motion.div
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ type: "spring", duration: 0.8 }}
          className="flex justify-center mb-12 relative"
        >
          <div
            className="relative w-32 h-32 sm:w-40 sm:h-40 lg:w-48 lg:h-48 flex items-center justify-center rounded-full bg-black/50 border theme-transition"
            style={{
              borderColor: currentTheme.primary + '40',
              boxShadow: `0 0 40px ${currentTheme.glow}`
            }}
            data-testid="octocat-icon"
          >
            <BubbleEffect isActive={analyzing} themeColor={currentTheme.primary} speed={bubbleSpeed} />

            {analyzing && (
              <div className="absolute inset-0 rounded-full overflow-hidden">
                <motion.div
                  className="absolute inset-0 animate-scan"
                  style={{
                    background: `linear-gradient(to bottom, ${currentTheme.primary}40, transparent)`
                  }}
                />
              </div>
            )}

            <Github
              className="w-20 h-20 sm:w-24 sm:h-24 lg:w-28 lg:h-28 octocat-glow theme-transition z-10"
              style={{ color: currentTheme.primary }}
            />

            {analyzing && (
              <motion.div
                animate={{ rotate: 360 }}
                transition={{ repeat: Infinity, duration: 2, ease: "linear" }}
                className="absolute inset-0 rounded-full border-2 border-transparent"
                style={{ borderTopColor: currentTheme.primary }}
              />
            )}
          </div>
        </motion.div>

        {/* Input Section */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
          className="glass-card rounded-2xl p-6 sm:p-8 mb-8"
        >
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 sm:gap-6 mb-6">
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-gray-500 mb-2">GitHub Repository URL</label>
              <input
                type="text"
                value={repoUrl}
                onChange={(e) => setRepoUrl(e.target.value)}
                placeholder="https://github.com/user/repo"
                className="w-full px-4 py-3 rounded-lg glass-input font-mono text-sm"
                style={{
                  borderColor: repoUrl ? currentTheme.primary + '40' : 'rgba(255, 255, 255, 0.1)',
                  boxShadow: repoUrl ? `0 0 10px ${currentTheme.glow}` : 'none'
                }}
                data-testid="repo-url-input"
              />
            </div>
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-gray-500 mb-2">Team Name</label>
              <input
                type="text"
                value={teamName}
                onChange={(e) => setTeamName(e.target.value)}
                placeholder="Cyberdyne Systems"
                className="w-full px-4 py-3 rounded-lg glass-input font-mono text-sm"
                style={{
                  borderColor: teamName ? currentTheme.primary + '40' : 'rgba(255, 255, 255, 0.1)',
                  boxShadow: teamName ? `0 0 10px ${currentTheme.glow}` : 'none'
                }}
                data-testid="team-name-input"
              />
            </div>
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-gray-500 mb-2">Team Leader Name</label>
              <input
                type="text"
                value={leaderName}
                onChange={(e) => setLeaderName(e.target.value)}
                placeholder="Sarah Connor"
                className="w-full px-4 py-3 rounded-lg glass-input font-mono text-sm"
                style={{
                  borderColor: leaderName ? currentTheme.primary + '40' : 'rgba(255, 255, 255, 0.1)',
                  boxShadow: leaderName ? `0 0 10px ${currentTheme.glow}` : 'none'
                }}
                data-testid="leader-name-input"
              />
            </div>
          </div>

          {error && (
            <div className="mb-4 p-4 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm font-mono">
              {error}
            </div>
          )}

          <button
            onClick={handleAnalyze}
            disabled={analyzing || !repoUrl || !teamName || !leaderName}
            className="w-full py-4 font-rajdhani font-bold text-lg border rounded-lg uppercase tracking-widest transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-3"
            style={{
              background: currentTheme.primary + '15',
              color: currentTheme.primary,
              borderColor: currentTheme.primary + '80',
              boxShadow: `0 0 20px ${currentTheme.glow}`
            }}
            data-testid="analyze-button"
          >
            {analyzing ? (
              <>
                <motion.div
                  animate={{ rotate: 360 }}
                  transition={{ repeat: Infinity, duration: 1, ease: "linear" }}
                >
                  <Sparkles className="w-5 h-5" />
                </motion.div>
                {uiState === 'error' ? 'Detecting Issues...' : 'Analyzing Repository...'}
              </>
            ) : (
              <>
                <Activity className="w-5 h-5" />
                Analyze Repository
              </>
            )}
          </button>
        </motion.div>

        {/* Results Section */}
        <AnimatePresence>
          {showResults && apiData && (
            <>
              {/* Counters Row */}
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8"
              >
                <div className="glass-card rounded-xl p-4">
                  <div className="flex items-center gap-3">
                    <AlertTriangle className="w-8 h-8" style={{ color: currentTheme.primary }} />
                    <div>
                      <p className="text-xs font-mono uppercase text-gray-500">Total Failures</p>
                      <p className="text-2xl font-rajdhani font-bold" style={{ color: currentTheme.primary }} data-testid="total-failures">
                        {apiData.total_failures_detected}
                      </p>
                    </div>
                  </div>
                </div>

                <div className="glass-card rounded-xl p-4">
                  <div className="flex items-center gap-3">
                    <CheckCircle className="w-8 h-8" style={{ color: currentTheme.primary }} />
                    <div>
                      <p className="text-xs font-mono uppercase text-gray-500">Fixes Applied</p>
                      <p className="text-2xl font-rajdhani font-bold" style={{ color: currentTheme.primary }} data-testid="total-fixes">
                        {apiData.total_fixes_applied}
                      </p>
                    </div>
                  </div>
                </div>

                <div className="glass-card rounded-xl p-4">
                  <div className="flex items-center gap-3">
                    <Clock className="w-8 h-8" style={{ color: currentTheme.primary }} />
                    <div>
                      <p className="text-xs font-mono uppercase text-gray-500">Total Time</p>
                      <p className="text-2xl font-rajdhani font-bold" style={{ color: currentTheme.primary }} data-testid="total-time">
                        {apiData.total_time_formatted}
                      </p>
                    </div>
                  </div>
                </div>

                <div className="glass-card rounded-xl p-4">
                  <div className="flex items-center gap-3">
                    <Shield className="w-8 h-8" style={{ color: cicdStatus === 'PASSED' ? currentTheme.primary : '#FF4444' }} />
                    <div>
                      <p className="text-xs font-mono uppercase text-gray-500">CI/CD Status</p>
                      <p
                        className="text-2xl font-rajdhani font-bold"
                        style={{ color: cicdStatus === 'PASSED' ? currentTheme.primary : '#FF4444' }}
                        data-testid="cicd-status-large"
                      >
                        {cicdStatus}
                      </p>
                    </div>
                  </div>
                </div>
              </motion.div>

              {/* Run Summary & Score */}
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8"
              >
                {/* Run Summary Card */}
                <div className="glass-card rounded-2xl p-6">
                  <h2
                    className="text-2xl font-rajdhani font-semibold mb-4 flex items-center gap-2"
                    style={{ color: currentTheme.primary }}
                  >
                    <GitBranch className="w-6 h-6" />
                    Run Summary
                  </h2>
                  <div className="space-y-3">
                    <div>
                      <p className="text-xs font-mono uppercase text-gray-500 mb-1">Repository</p>
                      <p className="text-sm font-mono text-gray-300 break-all" data-testid="analyzed-repo">{apiData.repo_url}</p>
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <p className="text-xs font-mono uppercase text-gray-500 mb-1">Team</p>
                        <p className="text-sm font-mono text-gray-300" data-testid="analyzed-team">{apiData.team_name}</p>
                      </div>
                      <div>
                        <p className="text-xs font-mono uppercase text-gray-500 mb-1">Leader</p>
                        <p className="text-sm font-mono text-gray-300" data-testid="analyzed-leader">{apiData.leader_name}</p>
                      </div>
                    </div>
                    <div>
                      <p className="text-xs font-mono uppercase text-gray-500 mb-1">Generated Branch</p>
                      <p
                        className="text-sm font-mono font-bold"
                        style={{ color: currentTheme.primary }}
                        data-testid="generated-branch"
                      >
                        {branchName}
                      </p>
                    </div>
                  </div>
                </div>

                {/* Score Breakdown Panel */}
                <div className="glass-card rounded-2xl p-6">
                  <h2
                    className="text-2xl font-rajdhani font-semibold mb-4 flex items-center gap-2"
                    style={{ color: currentTheme.primary }}
                  >
                    <Zap className="w-6 h-6" />
                    Score Breakdown
                  </h2>
                  <div className="flex flex-col items-center justify-center mb-6">
                    <div
                      className="text-6xl font-rajdhani font-bold mb-2"
                      style={{ color: currentTheme.primary }}
                      data-testid="total-score"
                    >
                      {apiData.final_score}
                    </div>
                    <div className="text-gray-400 text-sm font-mono">out of 100+</div>
                  </div>
                  <div className="space-y-4">
                    <div>
                      <div className="flex justify-between text-sm font-mono mb-2">
                        <span className="text-gray-400">Base Score</span>
                        <span className="text-white">{apiData.base_score}</span>
                      </div>
                      <Progress
                        value={apiData.base_score}
                        className="h-2"
                        style={{
                          backgroundColor: 'rgba(255, 255, 255, 0.1)'
                        }}
                      />
                    </div>
                    <div className="flex justify-between text-sm font-mono">
                      <span className="text-green-400">Speed Bonus {apiData.total_time_seconds < 300 ? '(<5 min)' : ''}</span>
                      <span className="text-green-400">+{apiData.speed_bonus}</span>
                    </div>
                    <div className="flex justify-between text-sm font-mono">
                      <span className="text-red-400">Efficiency Penalty ({apiData.total_commits} commits)</span>
                      <span className="text-red-400">{apiData.efficiency_penalty}</span>
                    </div>
                  </div>
                  <div className="mt-6 pt-6 border-t border-white/10">
                    <div className="text-xs font-mono text-gray-400">
                      Formula: Base ({apiData.base_score}) + Speed ({apiData.speed_bonus > 0 ? '+' : ''}{apiData.speed_bonus}) + Efficiency ({apiData.efficiency_penalty}) = {apiData.final_score}
                    </div>
                  </div>
                </div>
              </motion.div>

              {/* Fixes Applied Table */}
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.1 }}
                className="glass-card rounded-2xl p-6 mb-8"
              >
                <h2
                  className="text-2xl font-rajdhani font-semibold mb-6 flex items-center gap-2"
                  style={{ color: currentTheme.primary }}
                >
                  <AlertTriangle className="w-6 h-6" />
                  Fixes Applied
                </h2>
                <div className="overflow-x-auto">
                  <table className="w-full" data-testid="fixes-table">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left text-xs font-mono uppercase text-gray-500 tracking-wider pb-4 px-2">File</th>
                        <th className="text-left text-xs font-mono uppercase text-gray-500 tracking-wider pb-4 px-2">Bug Type</th>
                        <th className="text-left text-xs font-mono uppercase text-gray-500 tracking-wider pb-4 px-2">Line</th>
                        <th className="text-left text-xs font-mono uppercase text-gray-500 tracking-wider pb-4 px-2">Commit Message</th>
                        <th className="text-left text-xs font-mono uppercase text-gray-500 tracking-wider pb-4 px-2">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {apiData.fixes.map((fix, index) => (
                        <tr
                          key={index}
                          className="border-b border-white/5 hover:bg-white/5 transition-colors"
                          style={{
                            backgroundColor: fix.status === 'SUCCESS' ? 'rgba(0, 255, 136, 0.03)' : 'rgba(255, 68, 68, 0.03)'
                          }}
                        >
                          <td className="py-4 text-sm font-mono text-gray-300 px-2">{fix.file}</td>
                          <td className="py-4 px-2">
                            <span
                              className="inline-block px-2 py-1 rounded text-xs font-mono border"
                              style={{
                                background: currentTheme.primary + '15',
                                color: currentTheme.primary,
                                borderColor: currentTheme.primary + '40'
                              }}
                            >
                              {fix.type}
                            </span>
                          </td>
                          <td className="py-4 text-sm font-mono text-gray-300 px-2">{fix.line}</td>
                          <td className="py-4 text-sm font-mono text-gray-300 px-2">{fix.commit}</td>
                          <td className="py-4 px-2">
                            {fix.status === 'SUCCESS' ? (
                              <div className="flex items-center gap-2 text-green-400">
                                <CheckCircle className="w-4 h-4" />
                                <span className="text-xs font-mono">SUCCESS</span>
                              </div>
                            ) : (
                              <div className="flex items-center gap-2 text-red-400">
                                <XCircle className="w-4 h-4" />
                                <span className="text-xs font-mono">FAILED</span>
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </motion.div>

              {/* CI/CD Status Timeline */}
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.2 }}
                className="glass-card rounded-2xl p-6"
              >
                <h2
                  className="text-2xl font-rajdhani font-semibold mb-6 flex items-center gap-2"
                  style={{ color: currentTheme.primary }}
                >
                  <Clock className="w-6 h-6" />
                  CI/CD Status Timeline
                </h2>
                <div className="space-y-4" data-testid="cicd-timeline">
                  {apiData.timeline.map((item, index) => (
                    <motion.div
                      key={index}
                      initial={{ opacity: 0, x: -20 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: 0.08 * index }}
                      className="flex items-center gap-4 p-4 rounded-lg bg-black/40 border border-white/5 hover:border-white/10 transition-colors"
                    >
                      <div className="flex-shrink-0">
                        {item.status === 'PASSED' ? (
                          <div
                            className="w-12 h-12 rounded-full border flex items-center justify-center"
                            style={{
                              background: currentTheme.primary + '15',
                              borderColor: currentTheme.primary + '40'
                            }}
                          >
                            <CheckCircle className="w-6 h-6" style={{ color: currentTheme.primary }} />
                          </div>
                        ) : (
                          <div className="w-12 h-12 rounded-full bg-red-500/10 border border-red-500/20 flex items-center justify-center">
                            <XCircle className="w-6 h-6 text-red-400" />
                          </div>
                        )}
                      </div>
                      <div className="flex-grow">
                        <div className="flex items-center gap-3 mb-1">
                          <span className="text-lg font-rajdhani font-semibold text-white">Iteration {item.iteration}</span>
                          <span
                            className="px-3 py-1 rounded-full text-xs font-mono uppercase tracking-wider"
                            style={{
                              background: item.status === 'PASSED' ? currentTheme.primary + '15' : '#ef444415',
                              color: item.status === 'PASSED' ? currentTheme.primary : '#ef4444',
                              border: `1px solid ${item.status === 'PASSED' ? currentTheme.primary + '40' : '#ef444440'}`,
                              boxShadow: item.status === 'PASSED' ? `0 0 10px ${currentTheme.glow}` : '0 0 10px rgba(239, 68, 68, 0.2)'
                            }}
                          >
                            {item.status}
                          </span>
                        </div>
                        <div className="flex items-center gap-4 text-sm font-mono text-gray-400">
                          <span>{item.timestamp}</span>
                          <span>•</span>
                          <span>{item.time}</span>
                        </div>
                      </div>
                    </motion.div>
                  ))}
                </div>
              </motion.div>
            </>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
};