" ~/.vimrc — starter config linked by bootstrap.sh

set nocompatible
syntax on
filetype plugin indent on

set number
set relativenumber
set ruler
set showcmd
set wildmenu
set incsearch
set hlsearch
set ignorecase
set smartcase

set expandtab
set tabstop=4
set shiftwidth=4
set softtabstop=4
set autoindent

set backspace=indent,eol,start
set encoding=utf-8
set scrolloff=5
set mouse=a

" Clear search highlight with <leader><space>
nnoremap <leader><space> :nohlsearch<CR>

" Persistent undo
if has('persistent_undo')
    set undodir=~/.vim/undodir
    silent !mkdir -p ~/.vim/undodir > /dev/null 2>&1
    set undofile
endif

set noswapfile
set nobackup
