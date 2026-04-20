#!/usr/bin/env node

const fs = require('fs/promises');
const path = require('path');
const { spawn } = require('child_process');
const { PDFDocument } = require('pdf-lib');

function parseArgs(argv) {
  const args = {
    inputDir: '/Users/daandradec/pages_best_quality',
    outputDirName: 'output',
    engine: null,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const k = argv[i];
    const v = argv[i + 1];
    if (k === '--input-dir' && v) {
      args.inputDir = v;
      i += 1;
    } else if (k === '--output-dir-name' && v) {
      args.outputDirName = v;
      i += 1;
    } else if (k === '--engine' && v) {
      args.engine = v;
      i += 1;
    }
  }

  return args;
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

async function listWebpFiles(inputDir) {
  const entries = await fs.readdir(inputDir, { withFileTypes: true });
  return entries
    .filter((e) => e.isFile() && e.name.toLowerCase().endsWith('.webp'))
    .map((e) => e.name)
    .sort((a, b) => a.localeCompare(b, 'en', { sensitivity: 'base' }));
}

async function writePdfFromJpgs(jpgPaths, pdfPath) {
  if (jpgPaths.length === 0) throw new Error('No hay JPGs para generar PDF.');
  const pdf = await PDFDocument.create();

  for (const jp of jpgPaths) {
    const bytes = await fs.readFile(jp);
    const img = await pdf.embedJpg(bytes);
    const page = pdf.addPage([img.width, img.height]);
    page.drawImage(img, { x: 0, y: 0, width: img.width, height: img.height });
  }

  const out = await pdf.save();
  await fs.writeFile(pdfPath, out);
}

function requireOrThrow(moduleName, hint) {
  try {
    // eslint-disable-next-line global-require, import/no-dynamic-require
    return require(moduleName);
  } catch {
    throw new Error(`Falta dependencia '${moduleName}'. Instala: ${hint}`);
  }
}

async function runSharpEngine(webpNames, inputDir, engineDir) {
  const sharp = requireOrThrow('sharp', 'npm i sharp');
  await ensureDir(engineDir);

  const outJpgs = [];
  for (const name of webpNames) {
    const src = path.join(inputDir, name);
    const out = path.join(engineDir, `${path.parse(name).name}.jpg`);
    await sharp(src)
      .flatten({ background: { r: 255, g: 255, b: 255 } })
      .jpeg({ quality: 100, mozjpeg: false, chromaSubsampling: '4:4:4' })
      .toFile(out);
    outJpgs.push(out);
  }

  outJpgs.sort((a, b) => path.basename(a).localeCompare(path.basename(b), 'en', { sensitivity: 'base' }));
  await writePdfFromJpgs(outJpgs, path.join(engineDir, 'sharp.pdf'));
  return outJpgs.length;
}

async function runJimpEngine(webpNames, inputDir, engineDir) {
  const jimpModule = requireOrThrow('jimp', 'npm i jimp');
  const JimpCtor = jimpModule.Jimp || jimpModule;
  const sharp = requireOrThrow('sharp', 'npm i sharp');
  await ensureDir(engineDir);

  const outJpgs = [];
  const errors = [];

  for (const name of webpNames) {
    const src = path.join(inputDir, name);
    const out = path.join(engineDir, `${path.parse(name).name}.jpg`);

    try {
      let image;
      try {
        const jimpRead = jimpModule.read || JimpCtor.read;
        image = await jimpRead(src);
      } catch {
        const { data, info } = await sharp(src)
          .ensureAlpha()
          .raw()
          .toBuffer({ resolveWithObject: true });
        if (typeof JimpCtor.fromBitmap === 'function') {
          image = JimpCtor.fromBitmap({ data, width: info.width, height: info.height });
        } else {
          image = new JimpCtor({ data, width: info.width, height: info.height });
        }
      }

      if (image && image.bitmap && image.bitmap.data) {
        const buf = image.bitmap.data;
        for (let i = 0; i < buf.length; i += 4) {
          const a = buf[i + 3] / 255;
          buf[i] = Math.round(buf[i] * a + 255 * (1 - a));
          buf[i + 1] = Math.round(buf[i + 1] * a + 255 * (1 - a));
          buf[i + 2] = Math.round(buf[i + 2] * a + 255 * (1 - a));
          buf[i + 3] = 255;
        }
      }

      if (typeof image.quality === 'function') image.quality(100);

      const jpegMime = jimpModule.MIME_JPEG || 'image/jpeg';
      if (typeof image.getBufferAsync === 'function') {
        const b = await image.getBufferAsync(jpegMime);
        await fs.writeFile(out, b);
      } else if (typeof image.getBuffer === 'function') {
        const b = await new Promise((resolve, reject) => {
          image.getBuffer(jpegMime, (err, buf) => (err ? reject(err) : resolve(buf)));
        });
        await fs.writeFile(out, b);
      } else if (typeof image.writeAsync === 'function') {
        await image.writeAsync(out);
      } else {
        await new Promise((resolve, reject) => {
          image.write(out, (err) => (err ? reject(err) : resolve()));
        });
      }

      outJpgs.push(out);
    } catch (err) {
      errors.push(`${name}: ${String(err && err.message ? err.message : err)}`);
    }
  }

  if (outJpgs.length === 0) {
    throw new Error(`Jimp no convirtió imágenes. Ejemplo: ${errors[0] || 'sin detalle'}`);
  }

  outJpgs.sort((a, b) => path.basename(a).localeCompare(path.basename(b), 'en', { sensitivity: 'base' }));
  await writePdfFromJpgs(outJpgs, path.join(engineDir, 'jimp.pdf'));
  return outJpgs.length;
}

async function runSquooshEngine(webpNames, inputDir, engineDir) {
  const sharp = requireOrThrow('sharp', 'npm i sharp');
  let ImagePool;
  try {
    ({ ImagePool } = await import('@squoosh/lib'));
  } catch {
    ({ ImagePool } = requireOrThrow('@squoosh/lib', 'npm i @squoosh/lib'));
  }

  await ensureDir(engineDir);
  const pool = new ImagePool(1);
  const outJpgs = [];
  const errors = [];

  try {
    for (const name of webpNames) {
      const src = path.join(inputDir, name);
      const out = path.join(engineDir, `${path.parse(name).name}.jpg`);
      try {
        const inputPngWhiteBg = await sharp(src)
          .flatten({ background: { r: 255, g: 255, b: 255 } })
          .png()
          .toBuffer();

        const image = pool.ingestImage(inputPngWhiteBg);
        await image.decoded;
        await image.encode({
          mozjpeg: {
            quality: 100,
            baseline: false,
            progressive: false,
            optimizeCoding: true,
          },
        });
        const encoded = await image.encodedWith.mozjpeg;
        await fs.writeFile(out, Buffer.from(encoded.binary));
        outJpgs.push(out);
      } catch (err) {
        errors.push(`${name}: ${String(err && err.message ? err.message : err)}`);
      }
    }
  } finally {
    await pool.close();
  }

  if (outJpgs.length === 0) {
    throw new Error(`Squoosh no convirtió imágenes. Ejemplo: ${errors[0] || 'sin detalle'}`);
  }

  outJpgs.sort((a, b) => path.basename(a).localeCompare(path.basename(b), 'en', { sensitivity: 'base' }));
  await writePdfFromJpgs(outJpgs, path.join(engineDir, 'squoosh.pdf'));
  return outJpgs.length;
}

function engineRunner(name) {
  if (name === 'sharp') return runSharpEngine;
  if (name === 'jimp') return runJimpEngine;
  if (name === 'squoosh') return runSquooshEngine;
  throw new Error(`Engine no soportado: ${name}`);
}

async function runSingleEngine(engineName, inputDir, outputRoot, webpNames) {
  const runner = engineRunner(engineName);
  const engineDir = path.join(outputRoot, engineName);
  await ensureDir(engineDir);

  try {
    const count = await runner(webpNames, inputDir, engineDir);
    await fs.writeFile(path.join(engineDir, 'RESULT.json'), JSON.stringify({ ok: true, count }, null, 2), 'utf8');
    console.log(`[${engineName}] OK | JPG=${count}`);
    return 0;
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    await fs.writeFile(path.join(engineDir, 'ERROR.txt'), `${msg}\n`, 'utf8');
    await fs.writeFile(path.join(engineDir, 'RESULT.json'), JSON.stringify({ ok: false, error: msg }, null, 2), 'utf8');
    console.error(`[${engineName}] ERROR: ${msg}`);
    return 2;
  }
}

function runChildEngine(scriptPath, engineName, inputDir, outputDirName) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [
      scriptPath,
      '--engine',
      engineName,
      '--input-dir',
      inputDir,
      '--output-dir-name',
      outputDirName,
    ], {
      stdio: 'inherit',
    });

    child.on('exit', (code, signal) => {
      resolve({ code: code ?? 1, signal: signal || null });
    });
  });
}

async function main() {
  const args = parseArgs(process.argv);
  const inputDir = path.resolve(args.inputDir);
  const outputRoot = path.join(inputDir, args.outputDirName);

  const stat = await fs.stat(inputDir).catch(() => null);
  if (!stat || !stat.isDirectory()) throw new Error(`input-dir no válido: ${inputDir}`);

  const webpNames = await listWebpFiles(inputDir);
  if (webpNames.length === 0) throw new Error(`No se encontraron .webp en ${inputDir}`);

  await ensureDir(outputRoot);

  if (args.engine) {
    const code = await runSingleEngine(args.engine, inputDir, outputRoot, webpNames);
    process.exit(code);
    return;
  }

  console.log(`WEBP detectados: ${webpNames.length}`);
  console.log(`Salida: ${outputRoot}`);

  const scriptPath = path.resolve(__filename);
  const engines = ['sharp', 'jimp', 'squoosh'];

  for (const eng of engines) {
    const engDir = path.join(outputRoot, eng);
    await ensureDir(engDir);
    console.log(`\n[${eng}] convirtiendo...`);
    const result = await runChildEngine(scriptPath, eng, inputDir, args.outputDirName);
    if (result.signal) {
      const msg = `Proceso ${eng} terminado por señal: ${result.signal}`;
      await fs.writeFile(path.join(engDir, 'ERROR.txt'), `${msg}\n`, 'utf8');
      console.error(`[${eng}] ${msg}`);
    }
  }

  console.log('\nProceso finalizado. Revisa cada carpeta y RESULT.json / ERROR.txt.');
}

main().catch((err) => {
  console.error(String(err && err.message ? err.message : err));
  process.exit(1);
});
