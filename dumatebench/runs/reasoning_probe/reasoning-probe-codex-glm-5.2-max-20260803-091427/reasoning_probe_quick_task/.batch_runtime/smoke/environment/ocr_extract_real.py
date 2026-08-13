#!/usr/bin/env python3
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("output_base")
    args = parser.parse_args()
    raise SystemExit(subprocess.run(["/usr/bin/tesseract", args.image, args.output_base]).returncode)


if __name__ == "__main__":
    main()
