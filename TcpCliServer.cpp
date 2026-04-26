#include "TcpCliServer.hpp"

#include <QCoreApplication>
#include <QHostAddress>
#include <QDateTime>
#include <QCryptographicHash>
#include <QIODevice>
#include <QRegularExpression>
#include <QVector>
#include <QtMath>
#include <algorithm>
#include <cmath>

// ---------------------------------------------------------------------------
// CLI transcript log (exe directory, append-only)
// ---------------------------------------------------------------------------

void TcpCliServer::appendSingleCliLogLine(QString const& role, QString const& text)
{
    if (text.isEmpty())
        return;
    QString t = text;
    t.replace(QLatin1Char('\r'), QStringLiteral("\\r"));
    t.replace(QLatin1Char('\n'), QStringLiteral("\\n"));
    QString const displayLine = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)
        + QLatin1Char(' ') + role + QLatin1Char(' ') + t;
    if (m_cliLog.isOpen())
    {
        m_cliLog.write((displayLine + QLatin1Char('\n')).toUtf8());
        m_cliLog.flush();
    }
    Q_EMIT cliLogLineAppended(displayLine);
}

void TcpCliServer::appendCliLog(QString const& role, QString const& text)
{
    // One timestamp per physical line: split CR/LF so spectrum never logs as literal \r \n
    if (text.isEmpty())
        return;
    if (!text.contains(QLatin1Char('\r')) && !text.contains(QLatin1Char('\n')))
    {
        appendSingleCliLogLine(role, text);
        return;
    }
    QRegularExpression const reSplit(QStringLiteral(R"([\r\n]+)"));
    const QStringList parts = text.split(reSplit, Qt::SkipEmptyParts);
    for (QString const& part : parts)
    {
        if (!part.isEmpty())
            appendSingleCliLogLine(role, part);
    }
}

// ---------------------------------------------------------------------------
// Construction / listen
// ---------------------------------------------------------------------------

TcpCliServer::TcpCliServer(quint16 port, QString const& password,
                           QHostAddress bindAddress, QObject* parent)
    : QObject(parent)
    , m_password(password)
{
    m_cliLogPath = QCoreApplication::applicationDirPath()
        + QStringLiteral("/wsjtcb-cli.log");
    m_cliLog.setFileName(m_cliLogPath);
    if (!m_cliLog.open(QIODevice::Append))
    {
        qWarning("TcpCliServer: could not open CLI log %s: %s",
                 qPrintable(m_cliLogPath), qPrintable(m_cliLog.errorString()));
    }
    else
    {
        appendCliLog(QStringLiteral("SYS"), QStringLiteral("log opened ") + m_cliLogPath);
    }

    connect(&m_server, &QTcpServer::newConnection,
            this,      &TcpCliServer::onNewConnection);

    if (!m_server.listen(bindAddress, port))
    {
        qWarning("TcpCliServer: failed to listen on %s:%d: %s",
                 qPrintable(bindAddress.toString()), port,
                 qPrintable(m_server.errorString()));
    }
    else
    {
        qInfo("TcpCliServer: listening on %s:%d",
              qPrintable(bindAddress.toString()), port);
    }
}

TcpCliServer::~TcpCliServer()
{
    if (m_cliLog.isOpen())
    {
        appendCliLog(QStringLiteral("SYS"), QStringLiteral("TcpCliServer shutdown"));
        m_cliLog.close();
    }
}

bool TcpCliServer::isListening() const
{
    return m_server.isListening();
}

// ---------------------------------------------------------------------------
// Connection management
// ---------------------------------------------------------------------------

void TcpCliServer::onNewConnection()
{
    QTcpSocket* incoming = m_server.nextPendingConnection();

    if (m_client && m_client->state() == QAbstractSocket::ConnectedState)
    {
        // Reject second connection
        appendCliLog(QStringLiteral("OUT"),
                     QStringLiteral("ERR: already connected (duplicate client rejected)"));
        incoming->write("ERR: already connected\r\n");
        incoming->flush();
        incoming->disconnectFromHost();
        incoming->deleteLater();
        return;
    }

    m_client = incoming;
    m_state   = m_password.isEmpty() ? State::Idle : State::Unauthed;
    m_readBuf.clear();
    m_selectedFreq = 1200;
    m_selectedDecode.clear();
    m_selectedCountry.clear();

    connect(m_client, &QTcpSocket::disconnected,
            this,     &TcpCliServer::onClientDisconnected);
    connect(m_client, &QTcpSocket::readyRead,
            this,     &TcpCliServer::onReadyRead);

    appendCliLog(QStringLiteral("SYS"),
                 QStringLiteral("client connected %1:%2")
                     .arg(m_client->peerAddress().toString())
                     .arg(m_client->peerPort()));

    if (m_state == State::Unauthed)
    {
        // Coy one-character prompt: password is the first line, no welcome until it matches.
        appendCliLog(QStringLiteral("OUT"), QStringLiteral("? "));
        m_client->write("? ");
        m_client->flush();
    }
    else
    {
        sendWelcomeBanner();
        sendPrompt();
    }
}

void TcpCliServer::onClientDisconnected()
{
    if (m_client)
    {
        appendCliLog(QStringLiteral("SYS"),
                     QStringLiteral("client disconnected %1")
                         .arg(m_client->peerAddress().toString()));
        m_client->deleteLater();
        m_client = nullptr;
    }
    m_state        = State::Unauthed;
    m_selectedFreq = 1200;
    m_selectedDecode.clear();
    m_selectedCountry.clear();
    m_readBuf.clear();
    Q_EMIT clientDisconnected();
}

// ---------------------------------------------------------------------------
// Read loop
// ---------------------------------------------------------------------------

void TcpCliServer::onReadyRead()
{
    if (!m_client) return;

    m_readBuf += QString::fromUtf8(m_client->readAll());

    while (true)
    {
        int idx = m_readBuf.indexOf('\n');
        if (idx < 0) break;

        QString line = m_readBuf.left(idx).trimmed();
        m_readBuf    = m_readBuf.mid(idx + 1);

        if (!line.isEmpty())
        {
            if (m_state == State::Unauthed)
            {
                appendCliLog(QStringLiteral("IN"), QStringLiteral("***REDACTED*** (first-line password)"));
                tryFirstLinePassword(line);
            }
            else
            {
                appendCliLog(QStringLiteral("IN"), line);
                processLine(line);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Decode string helpers
// ---------------------------------------------------------------------------

static constexpr int kCliDecodeIndentCols = 11;

// Raw decode format (space-separated, skip empties) — from decoder; display omits p[0] time and p[4] mode (FT8-only CLI):
//   [0] time (HHMM or HHMMSS)
//   [1] snr
//   [2] dt
//   [3] audio-freq Hz
//   [4] mode (not shown in CLI text)
//   [5..] message words
//
// Returns the audio frequency field as an integer, or -1 on parse failure.
static int extractFreqFromDecode(QString const& raw)
{
    QStringList p = raw.trimmed().split(' ', Qt::SkipEmptyParts);
    if (p.size() < 4) return -1;
    bool ok;
    int f = p[3].toInt(&ok);
    return ok ? f : -1;
}

// FT8 on-the-air text is short; keep this small so the country column sits left of big empty gaps.
static constexpr int kCliMessageCols  = 20; // left-justified; longer text truncated; aligns country
static constexpr int kCliCountryCols = 16;

static QString formatMessageFieldForCli(QString m)
{
    m = m.trimmed();
    if (m.size() > kCliMessageCols)
        m = m.left(kCliMessageCols);
    return m.leftJustified(kCliMessageCols, QLatin1Char(' '));
}

static QString formatCountryField(QString c)
{
    c = c.trimmed();
    if (c.isEmpty())
        c = QString(QChar(0x2014)); // em dash — unknown / no DE call
    if (c.size() > kCliCountryCols)
        c = c.left(kCliCountryCols);
    return c.leftJustified(kCliCountryCols, QLatin1Char(' '));
}

// Returns "  [FFFF]  snr  dt  <padded message>  country" — fixed-width; mode (raw p[4]) omitted (FT8 CLI).
// Two-char mark before [freq]: "!" + space for CQ, else two spaces, so the "[" column stays bar-aligned.
// Message is left-justified in kCliMessageCols (truncated if needed) so the country column lines up.
// Raw line: time snr dt freq mode [words...]; freq is moved to a bracketed prefix, not shown twice.
static QString formatDecodeForDisplay(QString const& raw, QString const& country)
{
    QStringList p = raw.trimmed().split(' ', Qt::SkipEmptyParts);
    if (p.size() < 5)
        return raw.trimmed();

    bool okF, okS, okD;
    int const f  = p[3].toInt(&okF);
    int const snr = p[1].toInt(&okS);
    double const dt = p[2].toDouble(&okD);

    QString const freqField = okF
        ? QStringLiteral("[%1]").arg(QString::number(f).rightJustified(4, QLatin1Char(' ')))
        : QStringLiteral("[%1]").arg(p[3], 4, QLatin1Char(' '));

    QString const snrField = okS
        ? QString::number(snr).rightJustified(4, QLatin1Char(' '))
        : p[1].rightJustified(4, QLatin1Char(' '));

    QString const dtField = okD
        ? QString::number(dt, 'f', 1).rightJustified(4, QLatin1Char(' '))
        : p[2].rightJustified(4, QLatin1Char(' '));

    QString const msg = (p.size() > 5) ? p.mid(5).join(QLatin1Char(' ')) : QString();
    QString const markPrefix = msg.contains(QStringLiteral("CQ"), Qt::CaseInsensitive)
        ? QStringLiteral("! ")
        : QStringLiteral("  ");
    QString const freqWithMark = markPrefix + freqField;
    QString const msgCol = formatMessageFieldForCli(msg);
    QString const ccol = formatCountryField(country);

    // Two spaces after dt, fixed-width message, two spaces, fixed-width country
    return QStringLiteral("%1 %2 %3  %4  %5")
        .arg(freqWithMark, snrField, dtField, msgCol, ccol);
}

// ---------------------------------------------------------------------------
// select <Hz> — shared by select / answer <Hz> / cq <Hz>
// ---------------------------------------------------------------------------

bool TcpCliServer::trySelectAudioFreq(int freqHz)
{
    if (freqHz <= 0)
    {
        sendLine("ERR: usage: select <audio-freq-Hz>  (positive integer, e.g. 987)");
        return false;
    }
    if ((m_pendingNfa > 0 || m_pendingNfb > 0) &&
        (freqHz < m_pendingNfa || freqHz > m_pendingNfb))
    {
        sendLine(QString("ERR: %1 Hz is outside active window (%2\xe2\x80\x93%3 Hz)")
                 .arg(freqHz).arg(m_pendingNfa).arg(m_pendingNfb));
        return false;
    }

    m_selectedFreq = freqHz;
    m_selectedDecode.clear();
    m_selectedCountry.clear();
    for (int i = 0; i < m_lastDecodes.size(); ++i)
    {
        QString const& raw = m_lastDecodes[i];
        if (extractFreqFromDecode(raw) == freqHz)
        {
            m_selectedDecode = raw;
            if (i < m_lastDecodeCountries.size())
                m_selectedCountry = m_lastDecodeCountries[i];
            break;
        }
    }

    emit setTxAudioFreqSignal(freqHz);
    emit setRxAudioFreqSignal(freqHz);

    if (m_selectedDecode.isEmpty())
        sendLine(QString("OK: selected %1 Hz (no decode here — valid for cq)")
                 .arg(freqHz));
    else
        sendLine(QString("OK: selected %1")
                 .arg(formatDecodeForDisplay(m_selectedDecode, m_selectedCountry)));
    return true;
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

// h help · b bye · s status · f select · c cq · a answer · x stoptx
static void expandOneLetterAuthed(QString& cmd)
{
    if (cmd.size() != 1) return;
    switch (cmd[0].toLower().unicode()) {
    case u'h': cmd = QStringLiteral("help"); break;
    case u'b': cmd = QStringLiteral("bye"); break;
    case u's': cmd = QStringLiteral("status"); break;
    case u'f': cmd = QStringLiteral("select"); break;
    case u'a': cmd = QStringLiteral("answer"); break;
    case u'c': cmd = QStringLiteral("cq"); break;
    case u'x': cmd = QStringLiteral("stoptx"); break;
    default:   break;
    }
}

static QString cliHelpLine(QString const& commandCol, QString const& oneLetter, QString const& rest)
{
    // Fixed-width name column for alignment in monospaced terminals
    int constexpr kW = 24;
    return QString("  %1  (%2)  %3")
        .arg(commandCol.leftJustified(kW, QLatin1Char(' ')))
        .arg(oneLetter)
        .arg(rest);
}

void TcpCliServer::processLine(QString const& line)
{
    QStringList parts = line.split(' ', Qt::SkipEmptyParts);
    if (parts.isEmpty()) return;

    QString cmd = parts[0].toLower();

    // -----------------------------------------------------------------------
    // Commands
    // -----------------------------------------------------------------------

    // One-letter set: q=callsign, g=grid, o=odd (before generic expand: q is not “bye”)
    if (parts[0].size() == 1)
    {
        QChar const ch = parts[0][0].toLower();
        if (ch == u'q')
        {
            if (parts.size() < 2)
                sendLine("ERR: usage: q <CALL>  (same as: set callsign <CALL>)");
            else
            {
                QStringList p2{QStringLiteral("set"), QStringLiteral("callsign"), parts.mid(1).join(QLatin1Char(' '))};
                handleSet(p2);
            }
            sendPrompt();
            return;
        }
        if (ch == u'g')
        {
            if (parts.size() < 2)
                sendLine("ERR: usage: g <GRID>  (same as: set grid <GRID>)");
            else
            {
                QStringList p2{QStringLiteral("set"), QStringLiteral("grid"), parts.mid(1).join(QLatin1Char(' '))};
                handleSet(p2);
            }
            sendPrompt();
            return;
        }
        if (ch == u'o')
        {
            if (parts.size() < 2)
                sendLine("ERR: usage: o <on|off>  (same as: set odd <on|off>)");
            else
            {
                QStringList p2{QStringLiteral("set"), QStringLiteral("odd"), parts.mid(1).join(QLatin1Char(' '))};
                handleSet(p2);
            }
            sendPrompt();
            return;
        }
    }

    expandOneLetterAuthed(cmd);

    if (cmd == "help")
    {
        printHelp();
    }
    else if (cmd == "bye" || cmd == "quit" || cmd == "exit")
    {
        sendLine("BYE");
        m_client->flush();
        m_client->disconnectFromHost();
    }
    else if (cmd == "status")
    {
        printStatus();
    }
    else if (cmd == "set" && parts.size() >= 3)
    {
        handleSet(parts);
    }
    else if (cmd == "set")
    {
        sendLine("ERR: usage: set <key> <value>");
    }

    else if (cmd == "select")
    {
        if (parts.size() < 2)
        {
            sendLine("ERR: usage: select <audio-freq-Hz>");
        }
        else
        {
            bool ok;
            int freqHz = parts[1].toInt(&ok);
            if (!ok)
                sendLine("ERR: usage: select <audio-freq-Hz>  (positive integer, e.g. 987)");
            else
                trySelectAudioFreq(freqHz);
        }
    }
    else if (cmd == "cq")
    {
        if (parts.size() >= 2)
        {
            bool ok;
            int freqHz = parts[1].toInt(&ok);
            if (!ok || freqHz <= 0)
            {
                sendLine("ERR: usage: cq  OR  cq <audio-freq-Hz>  (positive integer)");
            }
            else if (!trySelectAudioFreq(freqHz))
            {
                // error already reported
            }
            else
            {
                emit setTxAudioFreqSignal(m_selectedFreq);
                emit startCQSignal();
                sendLine(QString("OK: CQ queued at %1 Hz — will TX at next slot boundary")
                         .arg(m_selectedFreq));
            }
        }
        else
        {
            emit setTxAudioFreqSignal(m_selectedFreq);
            emit startCQSignal();   // MainWindow enables auto at next slot boundary
            sendLine(QString("OK: CQ queued at %1 Hz — will TX at next slot boundary")
                     .arg(m_selectedFreq));
        }
    }
    else if (cmd == "stoptx")
    {
        emit stopTxSignal();
        sendLine("OK: TX stopped — use cq or answer to start a new sequence");
    }
    else if (cmd == "answer")
    {
        if (parts.size() >= 2)
        {
            bool ok;
            int freqHz = parts[1].toInt(&ok);
            if (!ok || freqHz <= 0)
            {
                sendLine("ERR: usage: answer  OR  answer <audio-freq-Hz>  (positive integer)");
                sendPrompt();
                return;
            }
            if (!trySelectAudioFreq(freqHz))
            {
                sendPrompt();
                return;
            }
        }

        if (m_selectedDecode.isEmpty())
        {
            sendLine(QString("ERR: no decode at %1 Hz — 'answer' requires a decoded station")
                     .arg(m_selectedFreq));
        }
        else
        {
            QStringList p = m_selectedDecode.trimmed().split(' ', Qt::SkipEmptyParts);
            if (p.size() < 5)
            {
                sendLine("ERR: cannot parse selected decode");
                sendPrompt();
                return;
            }

            QTime   t    = QTime::fromString(p[0], p[0].size() > 4 ? "hhmmss" : "hhmm");
            qint32  snr  = p[1].toInt();
            float   dt   = p[2].toFloat();
            quint32 freq = p[3].toUInt();
            QString mode = p[4];
            QString msg  = p.size() > 5 ? p.mid(5).join(' ') : QString{};

            emit replySignal(t, snr, dt, freq, mode, msg, false, 0);
            sendLine(QString("OK: answering %1")
                     .arg(formatDecodeForDisplay(m_selectedDecode, m_selectedCountry)));
        }
    }
    else
    {
        sendLine(QString("ERR: unknown command '%1' — type 'help'").arg(cmd));
    }

    sendPrompt();
}

// ---------------------------------------------------------------------------
// "set" sub-commands
// ---------------------------------------------------------------------------

void TcpCliServer::handleSet(QStringList const& parts)
{
    // parts[0] == "set", parts[1] == key, parts[2..] == value
    QString key   = parts[1].toLower();
    QString value = parts.mid(2).join(' ');

    if (key == "callsign")
    {
        if (value.isEmpty())
        {
            sendLine("ERR: callsign value required");
        }
        else
        {
            emit setCallsignSignal(value.toUpper());
            sendLine(QString("OK: callsign = %1").arg(value.toUpper()));
        }
    }
    else if (key == "grid")
    {
        if (value.isEmpty())
        {
            sendLine("ERR: grid locator value required");
        }
        else
        {
            emit setGridSignal(value.toUpper());
            sendLine(QString("OK: grid = %1").arg(value.toUpper()));
        }
    }
    else if (key == "odd")
    {
        bool enable = !(value == "off" || value == "0" || value == "false");
        emit setTxFirstSignal(enable);
        sendLine(QString("OK: odd time slot TX %1").arg(enable ? "enabled" : "disabled"));
    }
    else
    {
        sendLine(QString("ERR: unknown set key '%1'").arg(key));
    }
}

// ---------------------------------------------------------------------------
// Inbound spectrum + decode data from MainWindow
// ---------------------------------------------------------------------------

void TcpCliServer::onTxStart(QString message)
{
    if (m_state == State::Unauthed || !m_client) return;
    m_lastTxMessage = message;
    QString const ts = QDateTime::currentDateTimeUtc().toString("hh:mm:ss");
    sendLineAfterNewline(QString("[%1] !!! TX: %2").arg(ts).arg(message));
    sendPrompt();
}

void TcpCliServer::onTxStop()
{
    if (m_state == State::Unauthed || !m_client) return;
    QString const ts = QDateTime::currentDateTimeUtc().toString("hh:mm:ss");
    if (m_lastTxMessage.isEmpty())
        sendLineAfterNewline(QString("[%1] !!! TX STOP").arg(ts));
    else
        sendLineAfterNewline(QString("[%1] !!! TX STOP (%2)").arg(ts).arg(m_lastTxMessage));
    m_lastTxMessage.clear();
    sendPrompt();
}

void TcpCliServer::onTxFirstChanged(bool txFirst)
{
    m_txFirst = txFirst;
}

void TcpCliServer::setStationSnapshot(QString const& callsign, QString const& grid)
{
    m_myCallsign = callsign.trimmed();
    m_myGrid     = grid.trimmed();
}

void TcpCliServer::onSpectrum(QVector<float> savg, float df3, int nfa, int nfb)
{
    m_pendingSpectrum  = savg;
    m_pendingDf3       = df3;
    m_pendingNfa       = nfa;
    m_pendingNfb       = nfb;
    m_spectrumPending  = true;
}

void TcpCliServer::onDecodes(QStringList decodes, QStringList countries)
{
    if (!m_client || m_state == State::Unauthed) return;

    m_lastDecodes = decodes;
    m_lastDecodeCountries = countries;
    while (m_lastDecodeCountries.size() < m_lastDecodes.size())
        m_lastDecodeCountries += QString();
    if (m_lastDecodeCountries.size() > m_lastDecodes.size())
        m_lastDecodeCountries = m_lastDecodeCountries.mid(0, m_lastDecodes.size());

    // Refresh m_selectedDecode against the new batch (freq selection persists;
    // decode at that freq may appear or disappear each period).
    if (m_selectedFreq >= 0)
    {
        m_selectedDecode.clear();
        m_selectedCountry.clear();
        for (int i = 0; i < m_lastDecodes.size(); ++i)
        {
            QString const& raw = m_lastDecodes[i];
            if (extractFreqFromDecode(raw) == m_selectedFreq)
            {
                m_selectedDecode = raw;
                if (i < m_lastDecodeCountries.size())
                    m_selectedCountry = m_lastDecodeCountries[i];
                break;
            }
        }
    }

    // Emit spectrum line
    if (m_spectrumPending && !m_pendingSpectrum.isEmpty())
    {
        // CR overwrites '> ' prompt, then bar + marker
        appendCliLog(QStringLiteral("OUT"), QStringLiteral("\\n"));
        m_client->write("\r\n");
        QString specBar, specMarker;
        renderSpectrumLines(m_pendingSpectrum, m_pendingDf3,
                            m_pendingNfa, m_pendingNfb,
                            m_selectedFreq, m_txFirst,
                            specBar, specMarker);
        sendLine(specBar);
        if (!specMarker.isEmpty())
            sendLine(specMarker);
        m_spectrumPending = false;
    }

    // Emit decode listing — keyed by audio frequency (indented under spectrum bar)
    QString const decodeIndent(kCliDecodeIndentCols, QLatin1Char(' '));
    for (int i = 0; i < decodes.size(); ++i)
    {
        QString const& c = (i < m_lastDecodeCountries.size()) ? m_lastDecodeCountries[i] : QString();
        sendLine(decodeIndent + formatDecodeForDisplay(decodes[i], c));
    }

    sendPrompt();
}

// ---------------------------------------------------------------------------
// Spectrum ASCII rendering
// ---------------------------------------------------------------------------

void TcpCliServer::renderSpectrumLines(QVector<float> const& savg,
                                        float df3, int nfa, int nfb,
                                        int selectedHz, bool txFirst,
                                        QString& barLine, QString& markerLine) const
{
    barLine.clear();
    markerLine.clear();

    // Build bar (spaces if no data)
    QString bar(m_spectrumWidth, ' ');

    if (!savg.isEmpty() && df3 > 0.f && nfb > nfa)
    {
        int lo = qMax(0,             static_cast<int>(nfa / df3));
        int hi = qMin(savg.size()-1, static_cast<int>(nfb / df3));
        if (hi <= lo) hi = qMin(savg.size()-1, lo + 1);
        int span = hi - lo;
        if (span < 1) span = 1;

        QVector<float> cols(m_spectrumWidth, -200.f);
        for (int bin = lo; bin <= hi; ++bin)
        {
            int col = static_cast<int>(
                static_cast<double>(bin - lo) / span * (m_spectrumWidth - 1));
            col = qBound(0, col, m_spectrumWidth - 1);
            cols[col] = qMax(cols[col], savg[bin]);
        }

        QVector<float> sorted = cols;
        std::sort(sorted.begin(), sorted.end());
        float noise = sorted[sorted.size() / 2];
        // savg is linear averaged power (symspec.f90), not dB — use ratio → dB
        constexpr float kFloor = 1e-30f;

        // dB above median (per column) — same basis as before
        QVector<float> db(m_spectrumWidth, -80.f);
        for (int c = 0; c < m_spectrumWidth; ++c)
        {
            float v = cols[c];
            if (v < kFloor) v = kFloor;
            float n = noise < kFloor ? kFloor : noise;
            db[c] = 10.f * std::log10(v / n);
        }

        // Per-burst normalisation: map the display column dBs into [0,1] using
        // robust percentiles (like a histogram stretch) so most of the passband
        // stays in the “quiet” ramp but strong bins still reach @ and █. A gamma > 1
        // darkens the mid range so plenty of space / weak glyphs remain.
        auto pct = [](QVector<float> s, float p) -> float
        {
            if (s.isEmpty()) return 0.f;
            if (p <= 0.f) return s.first();
            if (p >= 1.f) return s.last();
            double const pos = p * (s.size() - 1);
            int i0 = static_cast<int>(std::floor(pos));
            int i1 = qMin(i0 + 1, s.size() - 1);
            float const f = static_cast<float>(pos - i0);
            return s[i0] * (1.f - f) + s[i1] * f;
        };
        QVector<float> dbs = db;
        std::sort(dbs.begin(), dbs.end());
        constexpr float kPctLo  = 0.06f;  // noise / quiet band → bottom of map
        constexpr float kPctHi  = 0.88f;  // high but leave headroom for outliers
        constexpr float kMinDbSpan = 4.f; // avoid collapse when the row is almost flat
        constexpr float kGamma  = 1.22f;  // >1: more “dark space” in the band
        float pLo  = pct(dbs, kPctLo);
        float pHi  = pct(dbs, kPctHi);
        float dbSpan = std::max(pHi - pLo, kMinDbSpan);
        // Fallback: fixed dB range if something went odd
        constexpr float kDbLowFallback  = -4.f;
        constexpr float kDbHighFallback = 44.f;

        // 11-level ramp (weak → strong): [space] . - : = + * # % @ █
        static QString const kRamp = QStringLiteral(" .\u002D\u003A=+*#%@\u2588");

        bar.clear();
        bar.reserve(m_spectrumWidth);
        int const nR = kRamp.size();
        for (int c = 0; c < m_spectrumWidth; ++c)
        {
            float t = (db[c] - pLo) / dbSpan;
            if (!std::isfinite(t) || pHi <= pLo)
                t = (db[c] - kDbLowFallback) / (kDbHighFallback - kDbLowFallback);
            if (t < 0.f) t = 0.f;
            else if (t > 1.f) t = 1.f;
            float u = std::pow(t, kGamma);
            if (u < 0.f) u = 0.f;
            else if (u > 1.f) u = 1.f;
            int idx = static_cast<int>(u * static_cast<float>(nR - 1) + 0.5f);
            if (idx < 0) idx = 0;
            else if (idx >= nR) idx = nR - 1;
            bar.append(kRamp.at(idx));
        }
    }

    QString const ts = QDateTime::currentDateTimeUtc().toString("hh:mm:ss");
    barLine = QString("[%1] |%2|").arg(ts).arg(bar);

    // Marker line: '▲' aligned under selected freq, then "NHz, Slot"
    if (selectedHz >= 0 && nfb > nfa)
    {
        int const col = qBound(0, static_cast<int>(
            static_cast<double>(selectedHz - nfa) / (nfb - nfa) * (m_spectrumWidth - 1)),
            m_spectrumWidth - 1);
        markerLine = QString(12 + col, ' ')
            + QChar(0x25B2)  // ▲ UP-POINTING TRIANGLE
            + QString(" %1Hz, %2").arg(selectedHz).arg(txFirst ? "Odd" : "Even");
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

void TcpCliServer::sendLine(QString const& text)
{
    appendCliLog(QStringLiteral("OUT"), text);
    if (!m_client) return;
    m_client->write((text + "\r\n").toUtf8());
}

void TcpCliServer::sendLineAfterNewline(QString const& text)
{
    appendCliLog(QStringLiteral("OUT"), QStringLiteral("\\n"));
    if (!m_client) return;
    m_client->write("\r\n");
    sendLine(text);
}

void TcpCliServer::sendPrompt()
{
    appendCliLog(QStringLiteral("OUT"), QStringLiteral("> "));
    if (!m_client) return;
    m_client->write("> ");
    m_client->flush();
}

void TcpCliServer::sendWelcomeBanner()
{
    sendLine("WSJT-CB CLI ready");
    sendLine("Happy DX!");
    sendLine("Type 'help' (h) for one-letter and full commands");
}

void TcpCliServer::tryFirstLinePassword(QString const& line)
{
    auto h = [](QString const& s) {
        return QCryptographicHash::hash(s.toUtf8(), QCryptographicHash::Sha256);
    };
    QString const supplied = line.trimmed();
    if (h(supplied) == h(m_password))
    {
        m_state = State::Idle;
        sendWelcomeBanner();
        sendPrompt();
    }
    else
    {
        sendLine("ERR: wrong password");
        if (m_client)
        {
            m_client->flush();
            m_client->disconnectFromHost();
        }
    }
}

void TcpCliServer::printHelp()
{
    sendLine("Commands  (one letter = shortcut;  f 987  and  select 987  are the same):");
    sendLine(cliHelpLine(QStringLiteral("set callsign <CALL>"), QStringLiteral("q"), QStringLiteral("q <CALL>  -  set my callsign")));
    sendLine(cliHelpLine(QStringLiteral("set grid <GRID>"), QStringLiteral("g"), QStringLiteral("g <GRID>  -  set my grid")));
    sendLine(cliHelpLine(QStringLiteral("set odd <on|off>"), QStringLiteral("o"), QStringLiteral("o <on|off>  -  odd time slot (TX first)")));
    sendLine(cliHelpLine(QStringLiteral("stoptx"), QStringLiteral("x"), QStringLiteral("Stop TX and AutoSeq; cq/answer start a new sequence")));
    sendLine(cliHelpLine(QStringLiteral("status"), QStringLiteral("s"), QStringLiteral("Station, slot, offset, selection, decodes (pass)")));
    sendLine(cliHelpLine(QStringLiteral("select <Hz>"), QStringLiteral("f"), QStringLiteral("f <Hz>  -  Tx+Rx audio offset")));
    sendLine(cliHelpLine(QStringLiteral("cq [<Hz>]"), QStringLiteral("c"), QStringLiteral("CQ; c <Hz> selects that freq, then CQ")));
    sendLine(cliHelpLine(QStringLiteral("answer [<Hz>]"), QStringLiteral("a"), QStringLiteral("Answer; a <Hz> selects, then answer")));
    sendLine(cliHelpLine(QStringLiteral("help"), QStringLiteral("h"), QStringLiteral("This help text")));
    sendLine(cliHelpLine(QStringLiteral("bye"), QStringLiteral("b"), QStringLiteral("Close connection; quit and exit are aliases")));
}

void TcpCliServer::printStatus()
{
    QString const rule(44, QLatin1Char('-'));
    QString const callShow = m_myCallsign.isEmpty()
        ? QStringLiteral("(not set)")
        : m_myCallsign;
    QString const gridShow = m_myGrid.isEmpty()
        ? QStringLiteral("(not set)")
        : m_myGrid;
    QString const slotShow = m_txFirst
        ? QStringLiteral("Odd  (Tx first / odd FT8 cycle)")
        : QStringLiteral("Even (Tx second / even FT8 cycle)");
    QString const decode = m_selectedDecode.isEmpty()
        ? QStringLiteral("(none at this audio offset)")
        : formatDecodeForDisplay(m_selectedDecode, m_selectedCountry);

    sendLine(rule);
    sendLine(QStringLiteral(" status"));
    sendLine(rule);
    sendLine(QStringLiteral("  Callsign        %1").arg(callShow));
    sendLine(QStringLiteral("  Grid            %1").arg(gridShow));
    sendLine(QStringLiteral("  TX time slot    %1").arg(slotShow));
    sendLine(QStringLiteral("  Audio offset    %1 Hz  (Tx + Rx)").arg(m_selectedFreq));
    sendLine(QStringLiteral("  Selection       %1").arg(decode));
    sendLine(QStringLiteral("  Decodes (pass)  %1").arg(m_lastDecodes.size()));
    sendLine(rule);
}

void TcpCliServer::printSelectedSlot()
{
    if (m_selectedFreq < 0)
    {
        sendLine("nothing selected");
        return;
    }
    if (m_selectedDecode.isEmpty())
    {
        sendLine(QString("selected %1 Hz (no decode — valid for cq)").arg(m_selectedFreq));
        return;
    }
    sendLine(QString("selected: %1").arg(formatDecodeForDisplay(m_selectedDecode, m_selectedCountry)));
    QStringList p = m_selectedDecode.trimmed().split(' ', Qt::SkipEmptyParts);
    if (p.size() >= 5)
    {
        sendLine(QString("  time: %1  snr: %2 dB  dt: %3 s  freq: %4 Hz")
                 .arg(p[0]).arg(p[1]).arg(p[2]).arg(p[3]));
        if (p.size() > 5)
            sendLine(QString("  message: %1").arg(p.mid(5).join(' ')));
    }
}
